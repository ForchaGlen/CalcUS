import socket
import time

s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
while True:
    try:
        s.connect(('postgres', 5432))
        s.close()
        time.sleep(2)
        break
    except socket.error:
        time.sleep(1)
