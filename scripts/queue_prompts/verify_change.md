You check a code change written by another model against its acceptance criteria. Be strict: judge only what the diff shows. {{MET_WHEN}}

<<<BEGIN_TICKET>>>
{{TICKET}}
<<<END_TICKET>>>

The acceptance criteria, numbered:
{{CRITERIA}}

<<<BEGIN_DIFF>>>
{{DIFF}}
<<<END_DIFF>>>

<<<BEGIN_COMMIT_MESSAGES (written since the review, with the fixer's notes on items it left as they are)>>>
{{COMMITS}}
<<<END_COMMIT_MESSAGES>>>

<<<BEGIN_FIX_DIFF (what the fix itself changed since the review)>>>
{{FIX_DIFF}}
<<<END_FIX_DIFF>>>

For each criterion, decide whether the diff meets it, and copy the lines that show it: one to five lines copied character for character from the diff, without the leading + or -. Quote added lines; quote removed lines only for a criterion about removing something; unchanged lines, file names and the diff's header lines never count.

A criterion that starts with "(review)" is a reviewer's complaint. It is met when the diff fixes it, or when a commit message or the fixer's notes give a reason the complaint is wrong and the diff and ticket bear that reason out; then quote that reason. A reason that is not borne out does not count.

A criterion that starts with "(owner)" is the owner's own review comment on the diff. It is met when the diff does what it asks, or when a commit message or the fixer's notes say why it cannot be done as asked and the diff and ticket bear that out; then quote that reason. The owner's comments win over the plan: another criterion that such a comment changes is met when the diff does what the comment asks.

For a "(review)" or "(owner)" criterion you may also quote the fix diff, its removed lines too: a line the fix took out because the item asked for it shows the item is done.

Reply with only this JSON, no other text:
{"criteria": [{"n": 1, "met": true, "quote": "lines copied from the diff", "why": "one sentence"}],
 "verdict": "PASS" or "FAIL",
 "problems": ["one sentence per problem, such as a criterion not met or a change that breaks something; empty when PASS"]}

The verdict is PASS only if every criterion is met and the diff does nothing harmful the ticket did not ask for. Reply with the JSON only.
