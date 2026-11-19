// W2-F mock data: canned GET routes (key = path under /api/v1, plus ?query with params sorted and sig dropped;
// server.mjs falls back from path?query to path; /tickets/<K>/lines is generated) and one page of session messages
// (a copy of the recorded data/messages.json). A module, not JSON: Playwright loads server.mjs as CommonJS, where
// import.meta (needed to locate a data file) is not allowed.
export const VIEW_ROUTES = {
 "/jira": {
  "rows": [
   {
    "key": "DEMO-1",
    "summary": "Fix the login page",
    "status": "In Progress",
    "cat": "indeterminate",
    "assignee": "Me",
    "type": "Bug",
    "priority": "High",
    "queued": "queued"
   },
   {
    "key": "JIRA-11",
    "summary": "Add a dark theme",
    "status": "To Do",
    "cat": "new",
    "assignee": "Me",
    "type": "Story",
    "priority": "Medium",
    "queued": null
   },
   {
    "key": "JIRA-12",
    "summary": "Speed up the export",
    "status": "To Do",
    "cat": "new",
    "assignee": "Ann",
    "type": "Task",
    "priority": "Low",
    "queued": null
   },
   {
    "key": "JIRA-13",
    "summary": "Release notes for 2.0",
    "status": "Done",
    "cat": "done",
    "assignee": "",
    "type": "Task",
    "priority": "Low",
    "queued": null
   }
  ],
  "error": "",
  "note": "",
  "more": false,
  "projects": [
   "DEMO",
   "JIRA"
  ]
 },
 "/jira?list=sprint&mine=1": {
  "rows": [
   {
    "key": "DEMO-1",
    "summary": "Fix the login page",
    "status": "In Progress",
    "cat": "indeterminate",
    "assignee": "Me",
    "type": "Bug",
    "priority": "High",
    "queued": "queued"
   },
   {
    "key": "JIRA-11",
    "summary": "Add a dark theme",
    "status": "To Do",
    "cat": "new",
    "assignee": "Me",
    "type": "Story",
    "priority": "Medium",
    "queued": null
   }
  ],
  "error": "",
  "note": "",
  "more": false,
  "projects": [
   "DEMO",
   "JIRA"
  ]
 },
 "/jira?list=backlog": {
  "rows": [],
  "error": "",
  "note": "Showing an earlier list. Jira timed out.",
  "more": false,
  "projects": [
   "DEMO",
   "JIRA"
  ]
 },
 "/vaults": {
  "active": "demo",
  "dir": "/fake/vault",
  "vaults": [
   {
    "name": "demo",
    "url": "",
    "cloned": true
   },
   {
    "name": "notes",
    "url": "",
    "cloned": true
   }
  ]
 },
 "/vault/tree": {
  "entries": [
   {
    "name": "wiki",
    "path": "wiki",
    "dir": true,
    "size": 0
   },
   {
    "name": "img",
    "path": "img",
    "dir": true,
    "size": 0
   },
   {
    "name": "README.md",
    "path": "README.md",
    "dir": false,
    "size": 42
   }
  ]
 },
 "/vault/tree?path=wiki": {
  "entries": [
   {
    "name": "notes",
    "path": "wiki/notes",
    "dir": true,
    "size": 0
   },
   {
    "name": "index.md",
    "path": "wiki/index.md",
    "dir": false,
    "size": 220
   }
  ]
 },
 "/vault/tree?path=wiki/notes": {
  "entries": [
   {
    "name": "plan.md",
    "path": "wiki/notes/plan.md",
    "dir": false,
    "size": 30
   }
  ]
 },
 "/vault/tree?path=img": {
  "entries": [
   {
    "name": "pic.png",
    "path": "img/pic.png",
    "dir": false,
    "size": 68
   }
  ]
 },
 "/vault/file?path=wiki/index.md": {
  "path": "wiki/index.md",
  "kind": "markdown",
  "text": "# Index\n\nWelcome to the **demo** vault.\n\n- [The plan](notes/plan.md)\n- [[plan|Plan by wiki link]]\n- ![A picture](../img/pic.png)\n- [Outside](https://example.org/)\n\n```\n[[plan]] stays code\n```\n",
  "truncated": false
 },
 "/vault/file?path=wiki/notes/plan.md": {
  "path": "wiki/notes/plan.md",
  "kind": "markdown",
  "text": "# The plan\n\nShip it. Back to the [index](../index.md).\n",
  "truncated": false
 },
 "/vault/file?path=img/pic.png": {
  "path": "img/pic.png",
  "kind": "image",
  "text": "",
  "url": "/vault/file/img/pic.png"
 },
 "/vault/file?path=README.md": {
  "path": "README.md",
  "kind": "text",
  "text": "plain text readme\nsecond line\n",
  "truncated": true
 },
 "/debug/runs/DEMO-1-debug-1": {
  "text": "# Debug: login fails\n\n**Verdict:** bug\n\nThe session cookie is dropped on redirect.\n"
 },
 "/debug/reports/export-report": {
  "text": "# Export is slow\n\nNot a bug: the export waits on the network.\n"
 },
 "/tickets/DEMO-2/diff": {
  "repos": [
   {
    "rel": ".",
    "label": "demo"
   },
   {
    "rel": "lib",
    "label": "demo/lib"
   }
  ],
  "views": [
   "work",
   "uncommitted",
   "unpushed"
  ],
  "repo": ".",
  "view": "work",
  "diff": "diff --git a/src/app.ts b/src/app.ts\nindex 1111111..2222222 100644\n--- a/src/app.ts\n+++ b/src/app.ts\n@@ -20,4 +20,5 @@ export function start() {\n   const a = 1;\n-  const b = 2;\n+  const b = 3;\n+  const c = a + b;\n   return a;\n }\ndiff --git a/README.md b/README.md\nnew file mode 100644\n--- /dev/null\n+++ b/README.md\n@@ -0,0 +1,2 @@\n+# Demo\n+Hello.\n",
  "commits": [
   {
    "sha": "abcdef1234567890",
    "subject": "Fix start"
   }
  ],
  "notes": "",
  "says": "",
  "rev": "abcdef1",
  "kind": "git",
  "error": ""
 },
 "/tickets/DEMO-2/diff?repo=.&view=uncommitted": {
  "repos": [
   {
    "rel": ".",
    "label": "demo"
   },
   {
    "rel": "lib",
    "label": "demo/lib"
   }
  ],
  "views": [
   "work",
   "uncommitted",
   "unpushed"
  ],
  "repo": ".",
  "view": "uncommitted",
  "diff": "",
  "commits": [],
  "notes": "",
  "says": "",
  "rev": "",
  "kind": "git",
  "error": ""
 }
};

export const MESSAGES = {
 "count": 2,
 "from": 0,
 "live": false,
 "messages": [
  {
   "created": 1795071827364,
   "id": "msg_a1",
   "index": 0,
   "info": {
    "agent": "yolo",
    "model": {
     "modelID": "big",
     "providerID": "harness"
    },
    "role": "user"
   },
   "parts": [
    {
     "id": "prt_a10",
     "text": "fix it <script>alert(1)</script>",
     "type": "text"
    }
   ],
   "role": "user"
  },
  {
   "created": 1795071828364,
   "id": "msg_a2",
   "index": 1,
   "info": {
    "agent": "yolo",
    "modelID": "big",
    "providerID": "harness",
    "role": "assistant",
    "tokens": {
     "input": 900,
     "output": 40
    }
   },
   "parts": [
    {
     "id": "prt_a20",
     "type": "step-start"
    },
    {
     "id": "prt_a21",
     "text": "look at the parser first",
     "type": "reasoning"
    },
    {
     "callID": "c1",
     "id": "prt_a22",
     "state": {
      "input": {
       "command": "ls"
      },
      "output": "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
      "status": "completed",
      "time": {
       "end": 2001,
       "start": 1
      },
      "title": "ls"
     },
     "tool": "bash",
     "truncated": true,
     "type": "tool"
    },
    {
     "callID": "c2",
     "id": "prt_a23",
     "state": {
      "input": {
       "prompt": "find it"
      },
      "metadata": {
       "sessionId": "ses_child1"
      },
      "output": "found",
      "status": "completed",
      "title": "explore"
     },
     "tool": "task",
     "type": "tool"
    },
    {
     "callID": "c3",
     "id": "prt_a24",
     "state": {
      "error": "no such file",
      "input": {
       "filePath": "/nope"
      },
      "status": "error"
     },
     "tool": "read",
     "type": "tool"
    },
    {
     "callID": "c4",
     "id": "prt_a25",
     "state": {
      "input": {
       "filePath": "src/parse.py",
       "newString": "new line",
       "oldString": "old <line>"
      },
      "metadata": {
       "diff": "--- src/parse.py\n+++ src/parse.py\n@@ -1 +1 @@\n-old <line>\n+new line\n"
      },
      "output": "Edit applied.",
      "status": "completed",
      "title": "src/parse.py"
     },
     "tool": "edit",
     "type": "tool"
    },
    {
     "callID": "c5",
     "id": "prt_a26",
     "state": {
      "input": {
       "todos": [
        {
         "content": "read the parser",
         "status": "completed"
        },
        {
         "content": "fix <the> bug",
         "status": "in_progress"
        }
       ]
      },
      "output": "[]",
      "status": "completed",
      "title": "1 todo"
     },
     "tool": "todowrite",
     "type": "tool"
    },
    {
     "id": "prt_a27",
     "text": "Done.",
     "type": "text"
    }
   ],
   "role": "assistant"
  }
 ],
 "working": true
};
