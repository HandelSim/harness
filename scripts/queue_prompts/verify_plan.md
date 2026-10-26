You check a plan written by another model for a ticket. Be strict: a plan that is not about the ticket, invents requirements, or leaves out what the ticket asks for must FAIL.

<<<BEGIN_TICKET>>>
{{TICKET}}
<<<END_TICKET>>>

<<<BEGIN_PLAN>>>
{{PLAN}}
<<<END_PLAN>>>

The plan's acceptance criteria, numbered:
{{CRITERIA}}

Check:
1. Each criterion is something the ticket asks for or clearly needs, and a reader could check it by looking at a diff.
2. Together, the criteria cover everything the ticket asks for.
3. The approach is concrete (it names files and functions) and does no work the ticket does not ask for.

For each criterion, copy the exact words of the ticket it comes from (a phrase of at least a few words, copied character for character), or "" if the ticket has no such words.

Reply with only this JSON, no other text:
{"criteria": [{"n": 1, "ticket_quote": "exact words from the ticket", "ok": true, "why": "one sentence"}],
 "verdict": "PASS" or "FAIL",
 "problems": ["one sentence per problem; empty when PASS"]}

The verdict is PASS only if every criterion is ok and nothing the ticket asks for is missing. Reply with the JSON only.
