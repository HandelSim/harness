You were working on the {{STAGE}} stage of ticket {{KEY}} in this session. Its work did not pass the stage's check, so the ticket was blocked: {{WHY}}

<<<BEGIN_MESSAGE_FROM_THE_OWNER (it wins over everything before it)>>>
{{MESSAGE}}
<<<END_MESSAGE_FROM_THE_OWNER>>>

Go on with the same stage and fix what the check says. Your instructions for it are unchanged; the whole of them is in {{TASK_FILE}}: read it again before you go on if anything about them is unclear. Check what is already done (git status, git log, the files the stage writes) instead of starting over, put right what failed, then end the stage exactly as those instructions say.
