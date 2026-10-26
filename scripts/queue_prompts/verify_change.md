You check a code change written by another model against its acceptance criteria. Be strict: judge only what the diff shows. A criterion is met only when lines in the diff implement it; a comment, a TODO or a commit message saying it is done does not count.

<<<BEGIN_TICKET>>>
{{TICKET}}
<<<END_TICKET>>>

The acceptance criteria, numbered:
{{CRITERIA}}

<<<BEGIN_DIFF>>>
{{DIFF}}
<<<END_DIFF>>>

<<<BEGIN_COMMIT_MESSAGES (written since the review)>>>
{{COMMITS}}
<<<END_COMMIT_MESSAGES>>>

For each criterion, decide whether the diff meets it, and copy the lines that show it: one to five lines copied character for character from the diff, without the leading + or -. Quote added lines; quote removed lines only for a criterion about removing something; unchanged lines never count.

A criterion that starts with "(review)" is a reviewer's complaint. It is met when the diff fixes it, or when a commit message gives a reason the complaint is wrong and the diff and ticket bear that reason out; then quote that commit message. A reason that is not borne out does not count.

Reply with only this JSON, no other text:
{"criteria": [{"n": 1, "met": true, "quote": "lines copied from the diff", "why": "one sentence"}],
 "verdict": "PASS" or "FAIL",
 "problems": ["one sentence per problem, such as a criterion not met or a change that breaks something; empty when PASS"]}

The verdict is PASS only if every criterion is met and the diff does nothing harmful the ticket did not ask for. Reply with the JSON only.
