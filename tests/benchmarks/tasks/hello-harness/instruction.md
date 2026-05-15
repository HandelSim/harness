Create a file at `/app/hello.txt` containing exactly the text `hello-harness`
(no trailing newline required, but a single trailing newline is acceptable).

This is a wiring-test task — its purpose is to verify the benchmark stack
end-to-end (Harbor → harness → proxy → upstream → tools). Any solution
that produces the file will pass.
