from app.users import get_usr


def greet(user_id):
    name = get_usr(user_id)
    return "hello, %s" % name if name else "hello, stranger"
