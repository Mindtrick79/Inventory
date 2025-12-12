import smtplib
from email.message import EmailMessage

def test_office_email():
    # Office 365 SMTP settings
    smtp_config = {
        "host": "smtp.office365.com",
        "port": 587,
        "user": "office@robertspest.com",
        "password": "6362430900",
        "from_email": "office@robertspest.com",
        "to_email": "office@robertspest.com"
    }

    # Create the email
    msg = EmailMessage()
    msg["Subject"] = "Test Email from Office 365"
    msg["From"] = smtp_config["from_email"]
    msg["To"] = smtp_config["to_email"]
    msg.set_content("Test email from the inventory system")

    try:
        print(f"Connecting to {smtp_config['host']}...")
        with smtplib.SMTP(smtp_config["host"], smtp_config["port"]) as server:
            print(" Connected")
            server.starttls()
            print(" TLS started")
            print("Logging in...")
            server.login(smtp_config["user"], smtp_config["password"])
            print(" Logged in")
            print("Sending email...")
            server.send_message(msg)
            print(" Email sent!")
            return True
    except Exception as e:
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    test_office_email()
