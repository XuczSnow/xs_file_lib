import hashlib

print(
    hashlib.sha256(
        "admin123".encode()
    ).hexdigest()
)