You check a code change written by another model against its acceptance criteria. Be strict: judge only what the diff shows. A criterion is met only when lines in the diff implement it; a comment, a TODO or a commit message saying it is done does not count.

<<<BEGIN_TICKET>>>
{{TICKET}}
<<<END_TICKET>>>

The acceptance criteria, numbered:
{{CRITERIA}}

<<<BEGIN_DIFF>>>
{{DIFF}}
<<<END_DIFF>>>

For each criterion, decide whether the diff meets it, and copy the diff lines that show it: one to five lines copied character for character from the diff, without the leading + or -.

Reply with only this JSON, no other text:
{"criteria": [{"n": 1, "met": true, "quote": "lines copied from the diff", "why": "one sentence"}],
 "verdict": "PASS" or "FAIL",
 "problems": ["one sentence per problem, such as a criterion not met or a change that breaks something; empty when PASS"]}

The verdict is PASS only if every criterion is met and the diff does nothing harmful the ticket did not ask for. Reply with the JSON only.
