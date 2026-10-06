from app.greet import greet
from app.report import names
from app.users import get_user

assert get_user(2) == "grace"
assert greet(1) == "hello, ada"
assert greet(9) == "hello, stranger"
assert names([3, 9, 1]) == ["linus", "ada"]
print("all tests passed")
