import bcrypt

password = b"Raj1234"
hashed = bcrypt.hashpw(password, bcrypt.gensalt())

print(hashed.decode("utf-8").strip())  # clean hash for DB
