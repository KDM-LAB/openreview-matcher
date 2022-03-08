import math
import numpy as np
from sortedcontainers import SortedList
import time
import uuid
from .core import SolverException
import logging


class FairSequence(object):
    """
    Assign reviewers using a modified version of the Greedy Reviewer Round-Robin algorithm
    from I Will Have Order! Optimizing Orders for Fair Reviewer Assignment
    (https://arxiv.org/abs/2108.02126). This algorithm outputs an assignment that satisfies
    the weighted envy-free up to 1 item (WEF1) criterion. Each paper i has a "weight" equal to
    its demand for reviewers (k_i), and for all i, i's score for its own reviewers (v_i(A_i))
    is greater than or equal to (k_i/k_j)*v_i(A_j - r) for any paper j and some r given to j.

    Reviewers are assigned to papers one-by-one in priority order, with priority given to the papers with the
    lowest ratio of allocation size to demand. Ties in priority are resolved by assigning the reviewer-paper
    pair with the highest affinity. Some constraints apply to the selection process - most importantly,
    no paper can be assigned a reviewer that would cause a WEF1 violation. If this procedure fails to
    discover a complete, WEF1 allocation, we try the picking sequence again, allowing WEF1 violations
    during the process.
    """

    def __init__(
        self,
        minimums,
        maximums,
        demands,
        encoder,
        allow_zero_score_assignments=False,
        solution=None,
        logger=logging.getLogger(__name__),
    ):
        """
        Initialize a FairSequence matcher

        :param minimums: a list of integers specifying the minimum number of papers for each reviewer.
        :param maximums: a list of integers specifying the maximum number of papers for each reviewer.
        :param demands: a list of integers specifying the number of reviews required per paper.
        :param encoder: an Encoder class object used to get affinity and constraint matrices.
        :param allow_zero_score_assignments: bool to allow pairs with zero affinity in the solution.
            unknown matching scores default to 0. set to True to allow zero (unknown) affinity in solution.
        :param solution: a matrix of assignments (same shape as encoder.affinity_matrix)

        :return: initialized FairSequence matcher.
        """
        self.logger = logger
        self.allow_zero_score_assignments = allow_zero_score_assignments
        self.logger.debug("Init FairSequence")
        self.constraint_matrix = encoder.constraint_matrix.transpose()
        affinity_matrix = encoder.aggregate_score_matrix.transpose()

        self.maximums = np.array(maximums)
        self.minimums = np.array(minimums)
        self.demands = np.array(demands)

        self.affinity_matrix = affinity_matrix.copy()
        if not self.affinity_matrix.any():
            self.affinity_matrix = np.random.rand(*affinity_matrix.shape)

        self.orig_affinities = self.affinity_matrix.copy()

        self.num_reviewers = np.size(self.affinity_matrix, axis=0)
        self.num_papers = np.size(self.affinity_matrix, axis=1)

        if not self.allow_zero_score_assignments:
            # Find reviewers with no non-zero affinity edges after constraints are applied and remove their load_lb
            bad_affinity_reviewers = np.where(
                np.all(
                    (self.affinity_matrix * (self.constraint_matrix == 0))
                    == 0,
                    axis=1,
                )
            )[0]
            logging.debug(
                "Setting minimum load for {} reviewers to 0 "
                "because they do not have known affinity with any paper".format(
                    len(bad_affinity_reviewers)
                )
            )
            for rev_id in bad_affinity_reviewers:
                self.minimums[rev_id] = 0

        self.id = uuid.uuid4()
        self.solution = (
            solution
            if solution
            else np.zeros((self.num_reviewers, self.num_papers))
        )

        if self.affinity_matrix.shape != self.solution.shape:
            raise SolverException(
                "Affinity Matrix shape does not match the required shape. Affinity Matrix shape {}, expected shape {}".format(
                    self.affinity_matrix.shape, self.solution.shape
                )
            )

        self.best_revs = np.argsort(-1 * self.affinity_matrix, axis=0)
        self.max_affinity = np.max(self.affinity_matrix)
        self.safe_mode = True

        self.solved = False
        self.logger.debug("End Init FairSequence")

    def _validate_input_range(self):
        """Validate if demand is in the range of min supply and max supply."""
        self.logger.debug("Checking if demand is in range")

        min_supply = np.sum(self.minimums)
        max_supply = np.sum(self.maximums)
        demand = np.sum(self.demands)

        self.logger.debug(
            "Total demand is ({}), min review supply is ({}), and max review supply is ({})".format(
                demand, min_supply, max_supply
            )
        )

        if demand > max_supply or demand < min_supply:
            raise SolverException(
                "Total demand ({}) is out of range when min review supply is ({}) and max review supply is ({})".format(
                    demand, min_supply, max_supply
                )
            )

        self.logger.debug("Finished checking input ranges")

    def objective_val(self):
        """Get the objective value of the RAP."""
        return np.sum(self.sol_as_mat() * self.orig_affinities)

    def sol_as_mat(self):
        if self.solved:
            return self.solution
        else:
            raise SolverException(
                "You must have executed solve() before calling this function"
            )

    def _is_valid_assignment(self, r, p, dict_alloc, previous_attained_scores):
        """Ensure that we can assign reviewer r to paper p without breaking WEF1.

        We have to check any paper p_prime that has chosen a reviewer which is worth
        less to it than the value of r to p_prime. If p_prime has only chosen better
        reviewers, then it will necessarily be WEF1.

        Args:
            r - (int) the id of the reviewer we want to add
            p - (int) the id of the paper to which we are adding r
            dict_alloc - (dict) the current allocation, maps papers to lists of reviewers
            previous_attained_scores - (1d numpy array) maps each paper to the lowest affinity
                                        for any reviewer it has been assigned

        Returns:
            True if r can be assigned to p without violating WEF1, False otherwise.
        """
        papers_to_check_against = set()
        for rev in dict_alloc[p] + [r]:
            papers_to_check_against |= set(
                np.where(
                    previous_attained_scores < self.affinity_matrix[rev, :]
                )[0].tolist()
            )

        for p_prime in papers_to_check_against:
            # p_prime's value for p's bundle, if we add r and remove the max value, then divide by p_prime's demand
            p_alloc_r = dict_alloc[p] + [r]
            p_alloc_r_affin = self.affinity_matrix[
                p_alloc_r, [p_prime] * len(p_alloc_r)
            ].tolist()
            other = sum(p_alloc_r_affin)
            max_val = max(p_alloc_r_affin)
            other -= max_val
            other /= self.demands[p]

            # p_prime's value for own bundle, divided by p_prime's demand
            p_prime_alloc = dict_alloc[p_prime]
            p_prime_affin = self.affinity_matrix[
                p_prime_alloc, [p_prime] * len(p_prime_alloc)
            ].tolist()
            curr = sum(p_prime_affin)
            curr /= self.demands[p_prime]

            # check wef1
            if other > curr and not math.isclose(other, curr):
                return False

        return True

    def _select_next_paper(
        self,
        matrix_alloc,
        dict_alloc,
        best_revs_map,
        current_reviewer_maximums,
        previous_attained_scores,
        paper_priorities,
    ):
        """Select the next paper to be assigned a reviewer

        Each paper i has priority |A_i|/k_i, where A_i is the set of reviewers already
        assigned to paper i, and k_i is the total demand of paper i. The
        paper with lowest priority is chosen, with ties broken by selecting the paper which
        will select a reviewer with the highest affinity.
        For rationale, please see:
        Weighted Envy-Freeness in Indivisible Item Allocation by Chakraborty et al. 2020 and
        I Will Have Order! Optimizing Orders for Fair Reviewer Assignment by Payan and Zick 2021.

        Args:
            matrix_alloc - (2d numpy array) the assignment of reviewers to papers
            dict_alloc - (dict) the current allocation, maps papers to lists of reviewers
            best_revs_map - (dict) maps from papers to lists of reviewers in decreasing affinity order
            current_reviewer_maximums - (1d numpy array) number of papers a reviewer can still be assigned
            previous_attained_scores - (1d numpy array) maps each paper to the lowest affinity
                                        for any reviewer it has been assigned
            paper_priorities - (SortedList) list of tuples (priority, paper_id), sorted by increasing priority

        Returns:
            The index of the next paper to assign a reviewer, the index of the reviewer,
            and the updated map to the best remaining reviewers per paper.
        """
        min_priority = paper_priorities[0][0]
        choice_set = paper_priorities.irange(
            minimum=(min_priority, -1), maximum=(min_priority, self.num_papers)
        )

        next_paper = None
        next_rev = None
        next_mg = -10000

        for _, p in choice_set:
            removal_set = []
            for r in best_revs_map[p]:
                if (
                    current_reviewer_maximums[r] <= 0
                    or matrix_alloc[r, p] > 0.5
                    or self.constraint_matrix[r, p] != 0
                    or (
                        math.isclose(self.affinity_matrix[r, p], 0)
                        and not self.allow_zero_score_assignments
                    )
                ):
                    removal_set.append(r)
                elif self.affinity_matrix[r, p] > next_mg:
                    # This agent might be the greedy choice.
                    # Check if this is a valid assignment, then make it the greedy choice if so.
                    # If not a valid assignment, go to the next reviewer for this agent.
                    if not self.safe_mode or self._is_valid_assignment(
                        r, p, dict_alloc, previous_attained_scores
                    ):
                        next_paper = p
                        next_rev = r
                        next_mg = self.affinity_matrix[r, p]
                        break
                else:
                    # This agent cannot be the greedy choice
                    break

            for r in removal_set:
                best_revs_map[p].remove(r)

            if next_mg == self.max_affinity:
                return next_paper, next_rev, best_revs_map
        return next_paper, next_rev, best_revs_map

    def greedy_wef1(self):
        """Compute a WEF1 assignment via a picking sequence.

        Args:
            None

        Returns:
            A 2d numpy array with a WEF1 partial allocation to the papers.
            The array has the same shape as self.affinity_matrix, with a 1 in the i, j
            entry when reviewer i is assigned to paper j (and 0 otherwise).
        """
        matrix_alloc = np.zeros(self.affinity_matrix.shape, dtype=bool)
        dict_alloc = {p: list() for p in range(self.num_papers)}
        maximums_copy = self.maximums.copy()

        best_revs_map = {}
        for p in range(self.num_papers):
            best_revs_map[p] = self.best_revs[:, p].tolist()

        previous_attained_scores = np.ones(self.num_papers) * 1000

        paper_priorities = SortedList(
            [(0.0, p) for p in range(self.num_papers)]
        )

        remaining_demand = np.sum(self.demands)
        required_for_min = np.copy(self.minimums)
        demand_required_for_min = np.sum(required_for_min)
        been_restricted = False

        self.logger.debug("#info FairSequence:total paper demand is %d" % remaining_demand)
        start = time.time()

        while remaining_demand:
            if remaining_demand % 1000 == 0:
                self.logger.debug("#info FairSequence:remaining paper demand is %d" % remaining_demand)
                self.logger.debug("#info FairSequence:total time elapsed: %s s" % (time.time() - start))

            next_paper, next_rev, best_revs_map = self._select_next_paper(
                matrix_alloc,
                dict_alloc,
                best_revs_map,
                maximums_copy,
                previous_attained_scores,
                paper_priorities,
            )

            if next_paper is None:
                return None

            remaining_demand -= 1
            maximums_copy[next_rev] -= 1
            matrix_alloc[next_rev, next_paper] = 1
            dict_alloc[next_paper].append(next_rev)
            previous_attained_scores[next_paper] = min(
                self.affinity_matrix[next_rev, next_paper],
                previous_attained_scores[next_paper],
            )
            paper_priorities.remove((paper_priorities[0][0], next_paper))
            paper_priorities.add(
                (
                    len(dict_alloc[next_paper]) / self.demands[next_paper],
                    next_paper,
                )
            )

            if required_for_min[next_rev] > 0.1:
                required_for_min[next_rev] -= 1
                demand_required_for_min -= 1
            if (
                not been_restricted
                and demand_required_for_min >= remaining_demand
            ):
                self.logger.debug("#info FairSequence:remaining paper demand (%d) equals total remaining reviewer load LBs ("
                                  "%d), restricting reviewer supply" % (remaining_demand, demand_required_for_min))
                maximums_copy = np.copy(required_for_min)

        return matrix_alloc

    def solve(self):
        """Run a WEF1 assignment that maximizes the affinity at each step.

        Args:
            None

        Returns:
            The solution as a matrix.
        """

        self._validate_input_range()

        improper_papers = np.any(self.demands == 0)
        if improper_papers:
            proper_papers = np.where(self.demands > 0)[0]

            self.logger.debug("#info FairSequence:Found %d papers with 0 demand, removing them for now" % (self.num_papers - proper_papers.shape[0]))

            saved_demands = np.copy(self.demands)
            saved_constraint_matrix = np.copy(self.constraint_matrix)
            saved_affinity_matrix = np.copy(self.affinity_matrix)

            self.demands = self.demands[proper_papers]
            self.constraint_matrix = self.constraint_matrix[:, proper_papers]
            self.affinity_matrix = self.affinity_matrix[:, proper_papers]
            self.best_revs = self.best_revs[:, proper_papers]
            self.num_papers = proper_papers.size

        start = time.time()
        self.solution = self.greedy_wef1()
        self.logger.debug("#info FairSequence:greedy_wef1 took %s s" % (time.time() - start))

        if self.solution is None:
            self.logger.debug(
                "Unable to find a WEF1 allocation satisfying all papers' demands. "
                "Falling back to picking sequence without WEF1 guarantees."
            )
            self.safe_mode = False

            start = time.time()
            self.solution = self.greedy_wef1()
            self.logger.debug("#info FairSequence:greedy_wef1 (safe_mode off) took %s s" % (time.time() - start))

            if self.solution is None:
                raise SolverException(
                    "Solver could not find a solution. Adjust your parameters."
                )

        if improper_papers:
            self.logger.debug("#info FairSequence:Adding back papers with 0 demand")
            self.demands = saved_demands
            self.constraint_matrix = saved_constraint_matrix
            self.affinity_matrix = saved_affinity_matrix

            n = self.affinity_matrix.shape[0]
            idx = 0
            soln = np.zeros(self.affinity_matrix.shape, dtype=bool)
            for i in range(n):
                if i in proper_papers:
                    soln[:, i] = self.solution[:, idx]
                    idx += 1
            self.solution = soln

        self.logger.debug(
            "#info FairSequence:objective score of solution is {}".format(np.sum(self.affinity_matrix * self.solution))
        )

        self.solved = True
        return self.sol_as_mat().transpose()
