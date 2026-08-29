import bcrypt

password = b"Prathamesh1234"
hashed = bcrypt.hashpw(password, bcrypt.gensalt())
print(hashed.decode())   # print hash string for DB
