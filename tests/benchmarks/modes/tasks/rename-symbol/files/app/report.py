from app import users


def names(ids):
    return [users.get_usr(i) for i in ids if users.get_usr(i)]
