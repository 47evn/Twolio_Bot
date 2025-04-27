from flask import Flask, request
from twilio.rest import Client
import google.generativeai as genai
import requests
import urllib.parse
import re
from datetime import datetime
import json
import os
from dotenv import load_dotenv
load_dotenv()

import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)



app = Flask(__name__)

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.getenv("TWILIO_WHATSAPP_NUMBER")

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
genai.configure(api_key=GOOGLE_API_KEY)

model = genai.GenerativeModel("gemini-1.5-flash")

# Twilio client
twilio_client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Config
group_id = "3"
AUTH_API_URL = "https://bi.siissoft.com/secureappointment/api/v1/auth/login"
PROFESSIONALS_API_URL = "https://bi.siissoft.com/secureappointment/api/v1/professionals"
USER_INFO_API_TEMPLATE = "https://bi.siissoft.com/secureappointment/api/v1/users/{}"
GROUP_INFO_API_TEMPLATE = "https://bi.siissoft.com/secureappointment/api/v1/groups/{}"
APPOINTMENTS_API_TEMPLATE = "https://bi.siissoft.com/secureappointment/api/v1/appointments/{}"
USER_PERSONAL_APPOINTMENTS = "https://bi.siissoft.com/secureappointment/api/v1/appointments/{}"


# In-memory session storage
user_sessions = {}
user_instructions = {}

# Instruction for Gemini
default_instruction = (
    "When Returning the list of professionals, or appointment return in a professional and organized way not the Json as it is"
    "You are a chatbot that will be used for booking appointments and for customers to ask about the company information. "
    "If I provide you all the user info, you will use it to respond to the user's request. "
    "If the user asks about the FRP or anything else, use the group and user info provided to answer correctly. "
    "If the user asks about their information, provide the response based on the `user_info` that has already been provided to you in this prompt — do not ask for it again. "
    "You will also be given a list of professionals (ID, alias, and other available fields). "
    "If the user asks about any professional (by name, ID, or alias), use this list to answer. "
    "You will also receive appointment details if available. Use them to answer appointment-related queries."
    "No need to greet me again after the first time with hi hello, just answer the question. "
    "If the professional is not in the list, then reply with saying: 'Professional isn't a part of this group. Please refer to this list and enlist all the professionals.' "
    "If the user wants to Book an Appointment, forward them the required details in this format:Provide the Following Details \nProfessional ID : <id> \nDate start : <date> \nTime Start : <time>. Then ask the user to provide these details so you can book an appointment for them."
    
    "Once you receive the above requirements, please reply back with the message in the following format add nothing else in the reply message just the following format: APPOINTMENT BOOK PROFESSIONAL ID <professionals id> DATESTART <year-month-day> TIMESTART <hour:minute> USERID <the user id of the user>."
    "IMPORTANT: Upon receiving Professional ID, Date, and Time from the user for booknging, reply exclusively with: APPOINTMENT BOOK PROFESSIONAL ID <professionals id> DATESTART <year-month-day> TIMESTART <hour:minute> USERID <the user's id>. Add no other information or text to this reply."
    "IMPORTANT: If the user directly provides booking details (e.g., Professional ID : 13, Date start : 2025-04-15, Time Start : 12:00), do NOT display professional schedules. Instead, immediately respond with the exact format stated above."

    "IMPORTANT: When the user provides the booking details (e.g., Professional ID : 13, Date start : 15-4-25, Time Start : 12:00), DO NOT just show the professional schedule, "
    "If the user asks about the following endpoints, respond with 'INFO: <endpoint>' in the order of the list. The bot should only respond with the endpoint and should not provide any other information:"
    "INFO: payments/security"
    "INFO: payments/topupsure"
    "INFO: payments/debit_card"
    "INFO: payments/postepay"
    "INFO: payments/my_balance"
    "INFO: payments/contact_info"
    "INFO: payments/statement_info"
    "INFO: reviews/make"
    "INFO: reviews/read"
    "INFO: professionals/bio"
    "INFO: payment_method/paypal"
    "INFO: payment_method/debit_card"
    "INFO: payment_method/phone_credit"
    "INFO: 899/cant_call"
    "INFO: debit_card/top_up"
    "INFO: debit_card/technical_problems"
    "INFO: end_user/welcome"
    "INFO: end_user/professional_disabled"
    "INFO: end_user/professional_changed_group"
    "INFO: end_user/cant_topup"
    "INFO: end_user/courses"
    "INFO: end_user/send_messages_academy"
    "INFO: end_user/is_service_free" 
    "INFO: error/generic"
)


# Helper Function to Format Slots Data for Twilio Message
def format_slots(slots_data):
    """
    Format the slot data to reduce the size and fit within the Twilio 1600 character limit.
    This function groups slots by hour and combines consecutive time slots.
    """
    formatted_slots = []
    
    # Sort the slots by date
    sorted_dates = sorted(slots_data.keys())

    for date in sorted_dates:
        times = sorted(slots_data[date])  # Sort the times for the current date
        
        # Group times by hour (e.g., 10:00-10:50 -> "10:00 - 10:50 Available")
        hour_groups = {}
        for time in times:
            hour = time[:5]  # Extract the hour part, e.g., "10:00"
            if hour not in hour_groups:
                hour_groups[hour] = []
            hour_groups[hour].append(time)
        
        # Format the hours and their intervals
        for hour, times in hour_groups.items():
            if len(times) == 6:  # Assume full hour is available (e.g., 10:00 to 10:50 is fully booked)
                formatted_slots.append(f"📅 {date}: {hour} is fully available.")
            else:
                # List intervals
                intervals = ", ".join(times)
                formatted_slots.append(f"📅 {date}: {hour} - {hour}: {intervals}")
    
    # Join all formatted slots into one string, making sure it doesn't exceed 1600 characters
    formatted_slots_text = "\n".join(formatted_slots)
    return formatted_slots_text[:1500]  # Trim to 1500 characters to stay within Twilio limit

def authenticate_user(sender_number):
    if sender_number in user_sessions:
        access_token = user_sessions[sender_number]["access_token"]
        print(f"🔐 [CACHE] Access token for {sender_number}: {access_token}")
        return access_token

    auth_payload = {"username": "bot@siissoft.it", "password": "Dana"}
    headers = {"Content-Type": "application/json"}

    try:
        auth_response = requests.post(AUTH_API_URL, json=auth_payload, headers=headers, verify=False )
        auth_response.raise_for_status()
        auth_data = auth_response.json()
        auth_info = auth_data.get("auth", {})

        access_token = auth_info.get("access_token")
        refresh_token = auth_info.get("refreshToken")

        if not access_token or not refresh_token:
            return None

        print(f"🔐 [NEW] Access token for {sender_number}: {access_token}")

        user_sessions[sender_number] = {
            "access_token": access_token,
            "refresh_token": refresh_token
        }

        return access_token
    except Exception as e:
        print(f"❌ Failed to authenticate: {e}")
        return None
# In-memory state for registration
registration_state = {}
registration_data = {}

# Helper: fetch user info with "USER NOT FOUND" handling
def fetch_user_info(user_number, access_token):
    """
    Fetch user info from the API.  
    If API returns 404 with {"message": "USER NOT FOUND."}, return {"error": "USER_NOT_FOUND"}.  
    Otherwise return the JSON or None on other failures.
    """
    encoded_number = urllib.parse.quote(user_number)
    url = USER_INFO_API_TEMPLATE.format(encoded_number)
    headers = {"Authorization": f"Bearer {access_token}"}

    try:
        response = requests.get(url, headers=headers, verify=False )
        # Handle explicit "USER NOT FOUND." message
        if response.status_code == 404:
            data = response.json()
            if data.get("message") == "USER NOT FOUND.":
                print(f"⚠️ User {user_number} not found.")
                return {"error": "USER_NOT_FOUND"}

        response.raise_for_status()
        print(f"✅ User info fetched for {user_number}")
        return response.json()

    except Exception as e:
        print(f"❌ Failed to fetch user info for {user_number}: {e}")
        return None

# Helper: register a new user via API endpoint
def register_user(user_data, access_token):
    url = "https://bi.siissoft.com/secureappointment/api/v1/users"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    }
    try:
        resp = requests.post(url, headers=headers, json=user_data)
        resp.raise_for_status()
        print("✅ User registered successfully.")
        return resp.json()
    except Exception as e:
        print(f"❌ Registration failed: {e}")
        return None
def fetch_info(endpoint, access_token):
    """
    Given an endpoint (e.g., payments/security), fetch the corresponding info from the API.
    """
    info_url = f"https://bi.siissoft.com/secureappointment/api/v1/info/{endpoint}"
    headers = {
        "Authorization": f"Bearer {access_token}"
    }

    try:
        response = requests.get(info_url, headers=headers, verify=False )
        response.raise_for_status()
        data = response.json()

        if data["status"] == 200:
            # Extract the 'message' from the response and return it
            return data.get("message", "No message available.")
        else:
            return "Sorry, I couldn't retrieve the information. Please try again later."
    except Exception as e:
        print(f"❌ Error fetching info from endpoint {endpoint}: {e}")
        return "There was an issue fetching the information. Please try again later."

def fetch_group_info(group_id, access_token):
    url = GROUP_INFO_API_TEMPLATE.format(group_id)
    headers = {"Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers, verify=False )
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"❌ Failed to fetch group info: {e}")
        return None

def fetch_professionals(access_token):
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json"
    } 
    payload = {
        "groupId": int(group_id)
    }

    try:
        # Use requests.request() to allow GET with body
        response = requests.request(
            "GET",
            PROFESSIONALS_API_URL,
            headers=headers,
            json=payload
        )
        response.raise_for_status()
        professionals = response.json().get("professionals", [])
        print(f"✅ Professionals fetched: {len(professionals)}")
        return professionals
    except Exception as e:
        print(f"❌ Failed to fetch professionals: {e}")
        return []

def fetch_appointments(user_id, access_token):
    url = APPOINTMENTS_API_TEMPLATE.format(user_id)  # The URL now contains user_id as part of the path.
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    
    # Prepare the request body to include groupId
    data = {
        "groupId": group_id
    }

    try:
        # Send GET request with body (for this specific API, using requests.request() allows a GET with a body)
        response = requests.get(url, headers=headers, json=data, verify=False )  # JSON body with groupId
        response.raise_for_status()
        
        appointments = response.json().get("appointments", [])
        print(f"📅 Appointments fetched: {len(appointments)}")
        return appointments
    except Exception as e:
        print(f"❌ Failed to fetch appointments: {e}")
        return []

# Helper function to handle appointment booking
from datetime import datetime

# Helper function to ensure date and time formats are correct
def format_date_time(date_str, time_str):
    """
    Ensure the date and time are in the correct format for the API (YYYY-MM-DD for date, HH:MM for time).
    """
    # Check and format the date to "YYYY-MM-DD"
    try:
        formatted_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(f"Invalid date format: {date_str}. Expected format: YYYY-MM-DD.")
    
    # Check and format the time to "HH:MM"
    try:
        formatted_time = datetime.strptime(time_str, "%H:%M").strftime("%H:%M")
    except ValueError:
        raise ValueError(f"Invalid time format: {time_str}. Expected format: HH:MM.")
    
    return formatted_date, formatted_time

# Helper function to handle appointment booking
def book_appointment(appointment_details, access_token):
    """Make a POST request to book the appointment with proper authorization."""
    appointment_url = "https://bi.siissoft.com/secureappointment/api/v1/appointments"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {access_token}"  # Include the access token in the Authorization header
    }

    try:
        response = requests.post(appointment_url, data=json.dumps(appointment_details), headers=headers)
        response.raise_for_status()
        print(f"✅ Appointment booked successfully: {response.text}")
        return True
    except requests.exceptions.HTTPError as http_err:
        # Catching 404 error specifically and responding accordingly
        if response.status_code == 404:
            print(f"❌ Failed to book appointment: 404 Not Found")
            return False
        else:
            print(f"❌ Failed to book appointment: {http_err}")
            return False
    except Exception as e:
        print(f"❌ Failed to book appointment: {e}")
        return False
def fetch_user_personal_appointments(user_id, access_token):
    """
    Fetch the personal appointments of a user using their user_id and access_token.
    """
    url = USER_PERSONAL_APPOINTMENTS.format(user_id)  # The URL now contains user_id as part of the path.
    headers = {
        "Authorization": f"Bearer {access_token}"
    }
    data = {
        "groupId": 3  # Static groupId as per the current requirements
    }

    try:
        # Send GET request with body (for this specific API, using requests.request() allows a GET with a body)
        response = requests.get(url, headers=headers, json=data, verify=False )  # JSON body with groupId
        response.raise_for_status()
        
        appointments = response.json().get("appointments", [])
        print(f"📅 User's personal appointments fetched: {len(appointments)}")
        return appointments
    except Exception as e:
        print(f"❌ Failed to fetch personal appointments: {e}")
        return []
@app.route("/whatsapp", methods=["POST"])
def whatsapp():
    incoming_msg = request.values.get("Body", "").strip()
    sender_number = request.values.get("From", "").replace("whatsapp:", "")
    print(f"📲 Message received from: {sender_number}")

    if not incoming_msg:
        return "No message received", 400

    reply_text = "Sorry, I couldn't understand your request."

    try:
        # Authenticate
        access_token = authenticate_user(sender_number)
        if not access_token:
            reply_text = "Authentication failed. Please try again."
        else:
            # Fetch user info
            user_info = fetch_user_info(sender_number, access_token)

            # Registration flow for unregistered users
            if isinstance(user_info, dict) and user_info.get("error") == "USER_NOT_FOUND":
                state = registration_state.get(sender_number)
                # Prompted to register
                if state == 'prompted':
                    if incoming_msg.lower().startswith("yes"):
                        registration_state[sender_number] = 'awaiting_form'
                        registration_data[sender_number] = {"phone": sender_number}
                        send_reply(sender_number,
                                  "Please fill out this form:\n"
                                  "Name:\n"
                                  "Surname:\n"
                                  "Alias:\n"
                                  "Email:\n"
                                  f"Phone number: {sender_number}")
                    else:
                        registration_state.pop(sender_number, None)
                        send_reply(sender_number, "Okay, if you change your mind, just let me know.")
                    return "Message sent", 200

                # Awaiting form submission
                if state == 'awaiting_form':
                    lines = incoming_msg.splitlines()
                    data = {}
                    for line in lines:
                        if ':' in line:
                            k, v = line.split(':', 1)
                            data[k.strip().lower()] = v.strip()
                    name = data.get('name')
                    surname = data.get('surname')
                    alias = data.get('alias')
                    email = data.get('email')
                    if not all([name, surname, alias, email]):
                        send_reply(sender_number, "Please provide all fields: Name, Surname, Alias, Email.")
                        return "Message sent", 200
                    payload = {
                        "groupId": 3,
                        "name": name,
                        "surname": surname,
                        "alias": alias,
                        "phone_number": sender_number,
                        "email": email
                    }
                    result = register_user(payload, access_token)
                    if result:
                        send_reply(sender_number, f"Registration successful! Welcome, {name}.")
                    else:
                        send_reply(sender_number, "Registration failed. Please try again later.")
                    registration_state.pop(sender_number, None)
                    registration_data.pop(sender_number, None)
                    return "Message sent", 200

                # First unregistered interaction: forward to Gemini
                prompt = f"{default_instruction}\n\nUser: {incoming_msg}"
                gemini_response = model.generate_content(prompt)
                temp_reply = gemini_response.text.strip()
                if temp_reply.startswith("INFO:"):
                    endpoint = temp_reply.split("INFO: ", 1)[1].strip()
                    info_msg = fetch_info(endpoint, access_token)
                    reply_text = info_msg
                else:
                    registration_state[sender_number] = 'prompted'
                    reply_text = "YOU ARE NOT REGISTERED. WANT TO REGISTER AS A NEW USER ?"
                send_reply(sender_number, reply_text)
                return "Message sent", 200

            # Registered-user flow (unchanged)
            group_info = fetch_group_info(group_id, access_token)
            professionals_list = fetch_professionals(access_token)

            appointments = []
            personal_appointments = []
            user_id = user_info.get("user", {}).get("id") if user_info else None

            if user_id:
                appointments = fetch_appointments(user_id, access_token)
                personal_appointments = fetch_user_personal_appointments(user_id, access_token)

            instruction = user_instructions.get(sender_number, default_instruction)
            full_prompt = (
                f"{instruction}\n\n"
                f"User Info:\n{user_info}\n\n"
                f"Group Info:\n{group_info}\n\n"
                f"Professionals List:\n{professionals_list}\n\n"
                f"Appointments:\n{appointments}\n\n"
                f"Personal Appointments:\n{personal_appointments}\n\n"
                f"User: {incoming_msg}"
            )
            gemini_response = model.generate_content(full_prompt)
            reply_text = gemini_response.text.strip()

            if reply_text.startswith("INFO:"):
                endpoint = reply_text.split("INFO: ", 1)[1].strip()
                reply_text = fetch_info(endpoint, access_token)

         

            appointment_match = re.match(
                r"APPOINTMENT BOOK PROFESSIONAL ID (\d+) DATESTART (\d{4}-\d{2}-\d{2}) TIMESTART (\d{2}:\d{2}) USERID (\d+)",
                reply_text
            )
            if appointment_match:
                pid, ds, ts, uid = appointment_match.groups()
                formatted_date, formatted_time = format_date_time(ds, ts)
                details = {
                    "groupId": group_id,
                    "professionalId": int(pid),
                    "userId": int(uid),
                    "dateStart": formatted_date,
                    "timeStart": formatted_time
                }
                success = book_appointment(details, access_token)
                reply_text = (
                    f"Your appointment has been successfully booked with Professional ID {pid} for {formatted_date} at {formatted_time}."
                    if success else
                    "The requested time slot is already occupied. Please choose another time."
                )
            else:
                slot_match = re.match(r"PROFESSIONAL SLOT NEEDED (\d+)", reply_text)
                if slot_match:
                    pid = slot_match.group(1)
                    slots_url = f"https://bi.siissoft.com/secureappointment/api/v1/slots/{group_id}?professionalId={pid}"
                    try:
                        resp = requests.get(slots_url, headers={"Authorization": f"Bearer {access_token}"})
                        resp.raise_for_status()
                        slots_data = resp.json().get("slots", {})
                        formatted = format_slots(slots_data)
                        reply_text = (
                            f"Here are the available slots for Professional ID {pid}:\n{formatted}"
                            if formatted else
                            "No available slots found for the selected professional."
                        )
                    except Exception as e:
                        print(f"❌ Failed to fetch slots: {e}")
                        reply_text = "Sorry, there was an error fetching available slots. Please try again later."

    except Exception as e:
        print(f"⚠️ Error handling message: {e}")
        reply_text = (
            "Oops! Something went wrong. Try again in a moment. "
            "Sorry, our system is facing trouble, but I'm here to help!"
        )

    try:
        send_reply(sender_number, reply_text)
        return "Message sent", 200
    except Exception as e:
        print(f"❌ Failed to send WhatsApp message: {e}")
        return "Failed to send message", 500


def send_reply(sender_number, reply_text):
    """
    Send one or more WhatsApp messages, splitting reply_text into
    1500-character chunks to avoid Twilio’s 1600-char limit.
    """
    max_len = 1599
    # split into chunks of at most max_len characters
    chunks = [reply_text[i : i + max_len] for i in range(0, len(reply_text), max_len)]
    for chunk in chunks:
        twilio_client.messages.create(
            body=chunk,
            from_=TWILIO_WHATSAPP_NUMBER,
            to=f"whatsapp:{sender_number}"
        )

if __name__ == "__main__":
    # Bind to 0.0.0.0 on the Railway-provided port (fallback to 5000 locally)
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 5000)),
        debug=True
    )
