import openreview
import matcher
import argparse
import json

if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('config')
    parser.add_argument('--baseurl', help="openreview base URL")
    parser.add_argument('--username')
    parser.add_argument('--password')

    args = parser.parse_args()

    with open(args.config) as f:
        config = json.load(f)

    ## Initialize the client library with username and password
    client = openreview.Client(baseurl=args.baseurl, username=args.username, password=args.password)
    print("connecting to", client.baseurl)

    # network calls
    submission_inv = client.get_invitation(config['paper_invitation'])

    metadata = list(openreview.tools.iterget_notes(client, invitation=config['metadata_invitation']))
    reviewers_group = client.get_group(config['match_group'])
    reviewer_ids = reviewers_group.members
    papers_by_forum = {p.forum: p for p in openreview.tools.iterget_notes(client, invitation=submission_inv.id)}

    # This could be set by hand if reviewers or papers have specific supplies/demands
    supplies = [config['max_papers']] * len(reviewer_ids)
    demands = [config['max_users']] * len(metadata)


    # instantiate the metadata encoder, and use it to instantiate a flow solver
    encoder = matcher.metadata.Encoder(metadata, config, reviewer_ids)
    flow_solver = matcher.Solver(supplies, demands, encoder.cost_matrix, encoder.constraint_matrix)

    solution = flow_solver.solve()

    # decode the solution matrix
    assignments_by_forum, alternates_by_forum = encoder.decode(solution)

    config_inv = client.get_invitation(config['config_invitation'])
    client.post_note(openreview.Note(**{
        'invitation': config_inv.id,
        'readers': config_inv.reply['readers']['values'],
        'writers': config_inv.reply['writers']['values'],
        'signatures': config_inv.reply['signatures']['values'],
        'content': config
    }))

    assignment_inv = client.get_invitation(config['assignment_invitation'])
    for forum, assignments in assignments_by_forum.items():
        client.post_note(openreview.Note.from_json({
            'forum': forum,
            'invitation': assignment_inv.id,
            'replyto': forum,
            'readers': assignment_inv.reply['readers']['values'],
            'writers': assignment_inv.reply['writers']['values'],
            'signatures': assignment_inv.reply['signatures']['values'],
            'content': {
                'label': config['label'],
                'assignedGroups': assignments,
                'alternateGroups': []
            }
        }))