import RPi.GPIO as GPIO
import time

PIN = 27
GPIO.setwarnings(False)
GPIO.setmode(GPIO.BCM)
GPIO.setup(PIN, GPIO.OUT)

print("GPIO27 ON (niveau bas si relais actif bas)")
GPIO.output(PIN, GPIO.LOW)
time.sleep(2)

print("GPIO27 OFF")
GPIO.output(PIN, GPIO.HIGH)

GPIO.cleanup(PIN)
