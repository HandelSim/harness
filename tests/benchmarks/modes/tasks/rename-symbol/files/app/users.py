USERS = {1: "ada", 2: "grace", 3: "linus"}


def get_usr(user_id):
    """Return the user name for an id, or None."""
    return USERS.get(user_id)
