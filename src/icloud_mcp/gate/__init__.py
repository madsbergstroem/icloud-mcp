"""Sendgate: the approval gate. An agent or an outside system can only ask; the owner approves; the server carries it out once.

This package holds what does not depend on any one service: the queue (outbox), the kinds of action and their executors
(actions), the external source (external), the interface of every source (source), approval_status (status),
approval_withdraw (withdraw), the activity log (audit) and push notes (push). Nothing here imports mail, calendar, contacts,
iMessage, the chat model, the Mac bridge, the server, the settings or a plugin; tests/test_provenance.py checks that.
See docs/SENDGATE.md.
"""
