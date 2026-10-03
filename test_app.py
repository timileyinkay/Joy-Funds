import http.client
import json
import os
import tempfile
import threading
import unittest
from urllib.parse import urlencode
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app


class AccountFlowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), app.JoyFundsHandler)
        cls.server_thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.server_thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.server_thread.join(timeout=5)

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.previous_users_file = app.USERS_FILE
        app.USERS_FILE = os.path.join(self.temp_dir.name, "users.json")
        with app.SESSION_LOCK:
            app.USER_SESSIONS.clear()
            app.ADMIN_SESSIONS.clear()

    def tearDown(self):
        app.USERS_FILE = self.previous_users_file
        with app.SESSION_LOCK:
            app.USER_SESSIONS.clear()
            app.ADMIN_SESSIONS.clear()
        self.temp_dir.cleanup()

    def request(self, method, path, body=None, cookie=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        headers = {}
        if body is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if cookie:
            headers["Cookie"] = cookie
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        result = (response.status, dict(response.getheaders()), payload)
        connection.close()
        return result

    def create_account(self, name, email, password="long-test-password-123"):
        return self.request(
            "POST",
            "/api/signup",
            urlencode({"full_name": name, "email": email, "password": password}),
        )

    def test_signup_creates_zero_balance_profile_and_secure_session(self):
        status, headers, body = self.create_account("Test User", "test@example.test")
        self.assertEqual(status, 200)
        cookie_header = headers["Set-Cookie"]
        self.assertIn("HttpOnly", cookie_header)
        self.assertIn("SameSite=Strict", cookie_header)
        cookie = cookie_header.split(";", 1)[0]

        status, _, profile_body = self.request("GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 200)
        profile = json.loads(profile_body)
        self.assertEqual(profile["full_name"], "Test User")
        self.assertEqual(profile["email"], "test@example.test")
        self.assertEqual(profile["balance_ngn"], 0)
        self.assertEqual(profile["activity"], [])
        self.assertEqual(profile["transactions"], [])
        self.assertEqual(profile["pending_doubling"], [])
        self.assertTrue(profile["created_at"])

        stored_users = app.load_users()
        self.assertEqual(stored_users["test@example.test"]["balance_ngn"], 0)
        self.assertNotEqual(stored_users["test@example.test"]["password"], "long-test-password-123")
        self.assertIn("pbkdf2_sha256$", stored_users["test@example.test"]["password"])

    def test_profile_isolation_between_accounts(self):
        self.create_account("First User", "first@example.test")
        _, second_headers, _ = self.create_account("Second User", "second@example.test")
        second_cookie = second_headers["Set-Cookie"].split(";", 1)[0]

        status, _, body = self.request("GET", "/api/me", cookie=second_cookie)
        self.assertEqual(status, 200)
        profile = json.loads(body)
        self.assertEqual(profile["email"], "second@example.test")
        self.assertEqual(profile["full_name"], "Second User")

    def test_admin_private_message_is_visible_only_to_recipient(self):
        _, first_headers, _ = self.create_account("First User", "first@example.test")
        _, second_headers, _ = self.create_account("Second User", "second@example.test")
        first_cookie = first_headers["Set-Cookie"].split(";", 1)[0]
        second_cookie = second_headers["Set-Cookie"].split(";", 1)[0]
        admin_cookie = f"{app.ADMIN_COOKIE}={app.create_admin_session()}"

        status, _, body = self.request(
            "POST",
            "/api/admin/user/message",
            urlencode({"email": "first@example.test", "message": "A private note for First User."}),
            cookie=admin_cookie,
        )
        self.assertEqual(status, 201, body.decode("utf-8"))

        status, _, body = self.request("GET", "/api/me", cookie=first_cookie)
        self.assertEqual(status, 200)
        first_profile = json.loads(body)
        self.assertEqual(first_profile["private_messages"][0]["message"], "A private note for First User.")

        status, _, body = self.request("GET", "/api/me", cookie=second_cookie)
        self.assertEqual(status, 200)
        second_profile = json.loads(body)
        self.assertEqual(second_profile["private_messages"], [])

        status, _, body = self.request(
            "POST",
            "/api/admin/user/message",
            urlencode({"email": "second@example.test", "message": "Forged message."}),
            cookie=first_cookie,
        )
        self.assertEqual(status, 401)

    def test_admin_private_message_rejects_empty_or_oversized_content(self):
        self.create_account("Message User", "message@example.test")
        admin_cookie = f"{app.ADMIN_COOKIE}={app.create_admin_session()}"

        for message in ("", "x" * 2001):
            status, _, _ = self.request(
                "POST",
                "/api/admin/user/message",
                urlencode({"email": "message@example.test", "message": message}),
                cookie=admin_cookie,
            )
            self.assertEqual(status, 400)

    def test_weak_password_rejected(self):
        status, _, body = self.create_account("Test User", "weak@example.test", "short")
        self.assertEqual(status, 400)
        self.assertIn("12 and 128", json.loads(body)["message"])
        self.assertEqual(app.load_users(), {})

    def test_login_returns_profile_for_existing_account(self):
        self.create_account("Returning User", "returning@example.test")
        status, headers, body = self.request(
            "POST",
            "/api/login",
            urlencode({"email": "returning@example.test", "password": "long-test-password-123"}),
        )
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request("GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 200)
        profile = json.loads(body)
        self.assertEqual(profile["full_name"], "Returning User")
        self.assertEqual(profile["balance_ngn"], 0)

    def test_legacy_account_migration_adds_zero_balance(self):
        app.save_users({
            "legacy@example.test": {
                "full_name": "Legacy User",
                "email": "legacy@example.test",
                "password": "legacy-password",
            }
        })
        app.migrate_user_passwords()
        migrated = app.load_users()["legacy@example.test"]
        self.assertEqual(migrated["balance_ngn"], 0)
        self.assertEqual(migrated["investments"], [])
        self.assertEqual(migrated["withdrawals"], [])
        self.assertIn("pbkdf2_sha256$", migrated["password"])

    def test_static_responses_include_security_headers(self):
        status, headers, _ = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")

    def test_admin_sees_accounts_without_password_or_bank_details(self):
        app.save_users({
            "member@example.test": {
                "full_name": "Member User",
                "email": "member@example.test",
                "password": "pbkdf2_sha256$salt$digest",
                "created_at": "2026-09-30T00:00:00Z",
                "balance_ngn": 0,
                "investments": [{"amount": 5000, "bank": "Private Bank", "account_number": "1234567890", "date": "2026-09-30"}],
                "withdrawals": [{"amount": 1000, "bank": "Private Bank", "account_number": "0987654321", "date": "2026-09-30"}],
            }
        })
        admin_token = app.create_admin_session()
        status, _, body = self.request(
            "GET", "/api/admin/users", cookie=f"{app.ADMIN_COOKIE}={admin_token}"
        )
        self.assertEqual(status, 200)
        serialized = body.decode("utf-8")
        self.assertNotIn("password", serialized)
        self.assertNotIn("account_number", serialized)
        self.assertNotIn("1234567890", serialized)
        self.assertNotIn("0987654321", serialized)
        result = json.loads(body)
        self.assertEqual(result["users"][0]["balance_ngn"], 0)
        self.assertEqual(len(result["users"][0]["activity"]), 2)

    def test_private_files_are_not_public(self):
        for path in ("/users.json", "/app.py", "/ai_brain.json"):
            with self.subTest(path=path):
                status, _, _ = self.request("GET", path)
                self.assertEqual(status, 404)

    def test_financial_actions_require_authenticated_sessions(self):
        _, headers, _ = self.create_account("Test User", "test@example.test")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        original_data = app.load_users()
        for path in ("/api/invest", "/api/withdraw"):
            with self.subTest(path=path):
                status, _, body = self.request("POST", path, "amount=1000")
                self.assertEqual(status, 401)
                self.assertFalse(json.loads(body)["success"])
        with patch.dict(os.environ, {}, clear=False):
            for setting in ("JOYFUNDS_DEPOSIT_BANK", "JOYFUNDS_DEPOSIT_ACCOUNT_NAME", "JOYFUNDS_DEPOSIT_ACCOUNT_NUMBER"):
                os.environ.pop(setting, None)
            status, _, body = self.request(
                "POST", "/api/invest", "amount=1000&sender_name=Test&transfer_reference=TX", cookie=cookie
            )
        self.assertEqual(status, 503)
        self.assertFalse(json.loads(body)["success"])
        self.assertEqual(app.load_users(), original_data)

    def test_deposit_notice_waits_for_configuration_and_admin_verification(self):
        _, signup_headers, _ = self.create_account("Deposit User", "deposit@example.test")
        user_cookie = signup_headers["Set-Cookie"].split(";", 1)[0]
        with patch.dict(os.environ, {}, clear=False):
            for setting in ("JOYFUNDS_DEPOSIT_BANK", "JOYFUNDS_DEPOSIT_ACCOUNT_NAME", "JOYFUNDS_DEPOSIT_ACCOUNT_NUMBER"):
                os.environ.pop(setting, None)
            status, _, body = self.request("GET", "/api/payment-details", cookie=user_cookie)
            self.assertEqual(status, 503)
            self.assertFalse(json.loads(body)["success"])

        config = {
            "JOYFUNDS_DEPOSIT_BANK": "Example Bank",
            "JOYFUNDS_DEPOSIT_ACCOUNT_NAME": "Example Account",
            "JOYFUNDS_DEPOSIT_ACCOUNT_NUMBER": "0001112223",
        }
        with patch.dict(os.environ, config):
            status, _, body = self.request("GET", "/api/payment-details", cookie=user_cookie)
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["account_number"], "0001112223")
            status, _, body = self.request(
                "POST",
                "/api/invest",
                urlencode({"amount": "5000", "sender_name": "Deposit User", "transfer_reference": "TX-EXAMPLE-1"}),
                cookie=user_cookie,
            )
        self.assertEqual(status, 201)
        self.assertIn("balance will not change", json.loads(body)["message"])

        admin_token = app.create_admin_session()
        status, _, body = self.request(
            "GET", "/api/admin/users", cookie=f"{app.ADMIN_COOKIE}={admin_token}"
        )
        self.assertEqual(status, 200)
        account = json.loads(body)["users"][0]
        self.assertEqual(account["activity"][0]["amount_ngn"], 5000)
        self.assertEqual(account["activity"][0]["sender_name"], "Deposit User")
        self.assertEqual(account["activity"][0]["transfer_reference"], "TX-EXAMPLE-1")
        self.assertEqual(account["activity"][0]["status"], "Pending payment verification")
        self.assertEqual(app.load_users()["deposit@example.test"]["balance_ngn"], 0)

    def test_withdrawal_request_collects_payout_details_matching_accepted_deposit_name(self):
        _, headers, _ = self.create_account("Withdrawal User", "withdrawal@example.test")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        users = app.load_users()
        users["withdrawal@example.test"]["balance_ngn"] = 5000
        users["withdrawal@example.test"]["investments"].append({
            "id": "accepted-deposit",
            "amount": 5000,
            "sender_name": "Deposit Account Holder",
            "funded_at": "2026-10-01T10:00:00Z",
            "accepted_at": "2026-10-01T10:00:00Z",
            "status": "Pending 4-day growth",
        })
        app.save_users(users)
        status, _, body = self.request(
            "POST", "/api/withdraw",
            urlencode({
                "amount": "2500",
                "note": "Please review this request.",
                "bank_name": "Example Bank",
                "account_name": "  deposit account holder ",
                "account_number": "0012345678",
            }),
            cookie=cookie,
        )
        self.assertEqual(status, 201)
        self.assertIn("added to pending", json.loads(body)["message"])

        admin_token = app.create_admin_session()
        status, _, body = self.request(
            "GET", "/api/admin/users", cookie=f"{app.ADMIN_COOKIE}={admin_token}"
        )
        self.assertEqual(status, 200)
        account = json.loads(body)["users"][0]
        request = account["activity"][0]
        self.assertEqual(request["type"], "Withdrawal request")
        self.assertEqual(request["amount_ngn"], 2500)
        self.assertEqual(request["note"], "Please review this request.")
        self.assertEqual(request["bank_name"], "Example Bank")
        self.assertEqual(request["account_name"], "deposit account holder")
        self.assertEqual(request["account_number"], "0012345678")

    def test_withdrawal_rejects_beneficiary_name_not_used_for_accepted_deposit(self):
        _, headers, _ = self.create_account("Withdrawal User", "name-check@example.test")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        users = app.load_users()
        users["name-check@example.test"]["balance_ngn"] = 5000
        users["name-check@example.test"]["investments"].append({
            "id": "accepted-deposit",
            "amount": 5000,
            "sender_name": "Deposit Account Holder",
            "funded_at": "2026-10-01T10:00:00Z",
            "accepted_at": "2026-10-01T10:00:00Z",
        })
        app.save_users(users)

        status, _, body = self.request(
            "POST", "/api/withdraw",
            urlencode({
                "amount": "1000",
                "bank_name": "Example Bank",
                "account_name": "Different Beneficiary",
                "account_number": "0012345678",
            }),
            cookie=cookie,
        )
        self.assertEqual(status, 400)
        self.assertIn("must match an accepted deposit name", json.loads(body)["message"])
        self.assertEqual(app.load_users()["name-check@example.test"]["balance_ngn"], 5000)

    def test_admin_credits_verified_deposit_once_with_audit_reference(self):
        _, signup_headers, _ = self.create_account("Credit User", "credit@example.test")
        user_cookie = signup_headers["Set-Cookie"].split(";", 1)[0]
        config = {
            "JOYFUNDS_DEPOSIT_BANK": "Example Bank",
            "JOYFUNDS_DEPOSIT_ACCOUNT_NAME": "Example Account",
            "JOYFUNDS_DEPOSIT_ACCOUNT_NUMBER": "0001112223",
        }
        with patch.dict(os.environ, config):
            status, _, body = self.request(
                "POST", "/api/invest",
                urlencode({"amount": "5000", "sender_name": "Credit User", "transfer_reference": "USER-REF-1"}),
                cookie=user_cookie,
            )
        self.assertEqual(status, 201)
        request_id = json.loads(body)["request_id"]
        admin_cookie = f"{app.ADMIN_COOKIE}={app.create_admin_session()}"
        review_body = urlencode({
            "request_id": request_id,
            "action": "credit_verified_deposit",
            "audit_reference": "BANK-STATEMENT-REF-88",
        })
        status, _, body = self.request("POST", "/api/admin/requests/action", review_body, cookie=admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["balance_ngn"], 5000)
        user = app.load_users()["credit@example.test"]
        self.assertEqual(user["balance_ngn"], 5000)
        self.assertEqual(len(user["ledger"]), 1)
        self.assertEqual(user["ledger"][0]["audit_reference"], "BANK-STATEMENT-REF-88")

        status, _, _ = self.request("POST", "/api/admin/requests/action", review_body, cookie=admin_cookie)
        self.assertEqual(status, 409)
        self.assertEqual(app.load_users()["credit@example.test"]["balance_ngn"], 5000)

    def test_admin_records_completed_withdrawal_without_double_debit_and_prevents_overdraw(self):
        _, signup_headers, _ = self.create_account("Payout User", "payout@example.test")
        user_cookie = signup_headers["Set-Cookie"].split(";", 1)[0]
        users = app.load_users()
        users["payout@example.test"]["balance_ngn"] = 7000
        app.save_users(users)
        status, _, body = self.request(
            "POST", "/api/withdraw",
            urlencode({
                "amount": "2500",
                "note": "Requested payout",
                "bank_name": "Example Bank",
                "account_name": "Payout User",
                "account_number": "0987654321",
            }),
            cookie=user_cookie,
        )
        self.assertEqual(status, 201)
        request_id = json.loads(body)["request_id"]
        admin_cookie = f"{app.ADMIN_COOKIE}={app.create_admin_session()}"
        review_body = urlencode({
            "request_id": request_id,
            "action": "mark_withdrawal_paid",
            "audit_reference": "PAYOUT-REF-12",
        })
        status, _, body = self.request("POST", "/api/admin/requests/action", review_body, cookie=admin_cookie)
        self.assertEqual(status, 200)
        user = app.load_users()["payout@example.test"]
        self.assertEqual(user["balance_ngn"], 4500)
        self.assertEqual(user["ledger"][0]["amount"], -2500)
        self.assertEqual(user["ledger"][0]["audit_reference"], "PAYOUT-REF-12")

        users = app.load_users()
        users["payout@example.test"]["balance_ngn"] = 0
        app.save_users(users)
        status, _, body = self.request(
            "POST",
            "/api/withdraw",
            urlencode({
                "amount": "1",
                "bank_name": "Example Bank",
                "account_name": "Payout User",
                "account_number": "0987654321",
            }),
            cookie=user_cookie,
        )
        self.assertEqual(status, 409)
        self.assertEqual(app.load_users()["payout@example.test"]["balance_ngn"], 0)

    def test_admin_deposit_review_accept_and_decline_do_not_require_audit_reference(self):
        _, signup_headers, _ = self.create_account("Review User", "review@example.test")
        user_cookie = signup_headers["Set-Cookie"].split(";", 1)[0]
        config = {
            "JOYFUNDS_DEPOSIT_BANK": "Example Bank",
            "JOYFUNDS_DEPOSIT_ACCOUNT_NAME": "Example Account",
            "JOYFUNDS_DEPOSIT_ACCOUNT_NUMBER": "0001112223",
        }
        with patch.dict(os.environ, config):
            status, _, body = self.request(
                "POST",
                "/api/invest",
                urlencode({"amount": "5000", "sender_name": "Review User", "transfer_reference": "USER-REF-2"}),
                cookie=user_cookie,
            )
        self.assertEqual(status, 201)
        request_id = json.loads(body)["request_id"]

        admin_cookie = f"{app.ADMIN_COOKIE}={app.create_admin_session()}"
        accept_body = urlencode({"request_id": request_id, "action": "accept_deposit"})
        status, _, body = self.request("POST", "/api/admin/requests/action", accept_body, cookie=admin_cookie)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["balance_ngn"], 5000)
        user = app.load_users()["review@example.test"]
        self.assertEqual(user["balance_ngn"], 5000)
        self.assertEqual(user["ledger"][0]["type"], "Deposit accepted")
        self.assertNotIn("audit_reference", user["ledger"][0])
        dashboard_profile = app.public_user_profile(user)
        self.assertFalse(any(item["type"] == "Deposit" for item in dashboard_profile["activity"]))
        self.assertEqual(dashboard_profile["pending_doubling_ngn"], 5000)
        self.assertEqual(dashboard_profile["pending_doubling"][0]["amount_ngn"], 5000)
        self.assertTrue(dashboard_profile["pending_doubling"][0]["due_at"])
        self.assertEqual(dashboard_profile["transactions"][0]["type"], "Deposit accepted")
        self.assertEqual(dashboard_profile["transactions"][0]["amount_ngn"], 5000)
        status, _, body = self.request("GET", "/api/me", cookie=user_cookie)
        self.assertEqual(status, 200)
        api_profile = json.loads(body)
        self.assertEqual(api_profile["pending_doubling_ngn"], 5000)
        self.assertTrue(api_profile["transactions"])

        status, _, body = self.request("POST", "/api/admin/requests/action", accept_body, cookie=admin_cookie)
        self.assertEqual(status, 409)
        self.assertEqual(app.load_users()["review@example.test"]["balance_ngn"], 5000)

        users = app.load_users()
        users["review@example.test"]["investments"][-1]["date"] = "2026-09-01T10:00:00Z"
        app.save_users(users)
        status, _, body = self.request(
            "POST",
            "/api/invest",
            urlencode({"amount": "5000", "sender_name": "Review User", "transfer_reference": "USER-REF-3"}),
            cookie=user_cookie,
        )
        self.assertEqual(status, 201)
        decline_request_id = json.loads(body)["request_id"]
        decline_body = urlencode({"request_id": decline_request_id, "action": "decline_deposit"})
        status, _, body = self.request("POST", "/api/admin/requests/action", decline_body, cookie=admin_cookie)
        self.assertEqual(status, 200)
        user = app.load_users()["review@example.test"]
        self.assertEqual(user["investments"][-1]["status"], "Declined")
        self.assertEqual(user["balance_ngn"], 5000)
        dashboard_profile = app.public_user_profile(user)
        self.assertFalse(any(item["type"] == "Deposit" for item in dashboard_profile["activity"]))
        declined_transaction = next(
            transaction for transaction in dashboard_profile["transactions"]
            if transaction["type"] == "Deposit"
        )
        self.assertEqual(declined_transaction["status"], "Declined")

    def test_investment_amount_must_match_or_exceed_last_accepted_deposit(self):
        _, headers, _ = self.create_account("Level User", "level@example.test")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        users = app.load_users()
        users["level@example.test"]["investments"].append({
            "id": "accepted-level-500",
            "amount": 500,
            "sender_name": "Level User",
            "date": "2026-09-01T10:00:00Z",
            "funded_at": "2026-09-01T10:00:00Z",
            "accepted_at": "2026-09-01T10:00:00Z",
            "status": "Pending 4-day growth",
        })
        app.save_users(users)

        status, _, body = self.request(
            "POST", "/api/invest",
            urlencode({"amount": "400", "sender_name": "Level User", "transfer_reference": "REF-LOW"}),
            cookie=cookie,
        )
        self.assertEqual(status, 400, body.decode("utf-8"))

        status, _, body = self.request(
            "POST", "/api/invest",
            urlencode({"amount": "500", "sender_name": "Level User", "transfer_reference": "REF-SAME"}),
            cookie=cookie,
        )
        self.assertEqual(status, 201, body.decode("utf-8"))

        status, _, body = self.request(
            "POST", "/api/invest",
            urlencode({"amount": "1000", "sender_name": "Level User", "transfer_reference": "REF-TOO-SOON"}),
            cookie=cookie,
        )
        self.assertEqual(status, 429, body.decode("utf-8"))

        users = app.load_users()
        users["level@example.test"]["investments"][-1]["date"] = "2026-09-01T10:00:00Z"
        app.save_users(users)
        status, _, body = self.request(
            "POST", "/api/invest",
            urlencode({"amount": "1000", "sender_name": "Level User", "transfer_reference": "REF-HIGH"}),
            cookie=cookie,
        )
        self.assertEqual(status, 201, body.decode("utf-8"))

    def test_deposit_cooldown_is_five_days_from_latest_request(self):
        _, headers, _ = self.create_account("Cooldown User", "cooldown@example.test")
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        status, _, body = self.request(
            "POST", "/api/invest",
            urlencode({"amount": "500", "sender_name": "Cooldown User", "transfer_reference": "FIRST"}),
            cookie=cookie,
        )
        self.assertEqual(status, 201, body.decode("utf-8"))

        status, _, body = self.request(
            "POST", "/api/invest",
            urlencode({"amount": "500", "sender_name": "Cooldown User", "transfer_reference": "SECOND"}),
            cookie=cookie,
        )
        self.assertEqual(status, 429)
        self.assertIn("five days", json.loads(body)["message"])

        status, _, body = self.request("GET", "/api/me", cookie=cookie)
        self.assertEqual(status, 200)
        profile = json.loads(body)
        self.assertTrue(profile["next_deposit_at"])
        self.assertGreater(profile["deposit_wait_seconds"], 0)

    def test_doubling_uses_progressive_growth_levels(self):
        for original, growth_total in [(500, 2000), (2000, 5000), (5000, 20000), (20000, 90000)]:
            user = {
                "balance_ngn": original,
                "investments": [{
                    "id": f"growth-{original}",
                    "amount": original,
                    "funded_at": "2020-01-01T00:00:00Z",
                    "doubled": False,
                    "status": "Pending 4-day growth",
                }],
                "ledger": [],
            }

            changed = app.process_pending_doubling(user)
            self.assertTrue(changed)
            self.assertEqual(user["balance_ngn"], growth_total)
            self.assertEqual(user["investments"][0]["status"], "Doubled")
            self.assertEqual(user["ledger"][0]["amount"], growth_total - original)

    def test_doubling_is_applied_once_after_the_configured_delay(self):
        user = {
            "balance_ngn": 500,
            "investments": [{
                "id": "old-funded-deposit",
                "amount": 500,
                "funded_at": "2020-01-01T00:00:00Z",
                "doubled": False,
                "status": "Pending 4-day growth",
            }],
            "ledger": [],
        }

        changed = app.process_pending_doubling(user)
        self.assertTrue(changed)
        self.assertEqual(user["balance_ngn"], 2000)
        self.assertEqual(user["investments"][0]["status"], "Doubled")
        self.assertEqual(len(user["ledger"]), 1)
        self.assertEqual(user["ledger"][0]["amount"], 1500)

        self.assertFalse(app.process_pending_doubling(user))
        self.assertEqual(user["balance_ngn"], 2000)
        self.assertEqual(len(user["ledger"]), 1)

    def test_admin_can_add_and_subtract_user_balance(self):
        self.create_account("Balance User", "balance@example.test")
        admin_cookie = f"{app.ADMIN_COOKIE}={app.create_admin_session()}"

        status, _, body = self.request(
            "POST",
            "/api/admin/user/balance",
            urlencode({"email": "balance@example.test", "mode": "add", "amount": "2500", "note": "Manual credit"}),
            cookie=admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["balance_ngn"], 2500)

        status, _, body = self.request(
            "POST",
            "/api/admin/user/balance",
            urlencode({"email": "balance@example.test", "mode": "subtract", "amount": "500", "note": "Correction"}),
            cookie=admin_cookie,
        )
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["balance_ngn"], 2000)

        user = app.load_users()["balance@example.test"]
        self.assertEqual(user["balance_ngn"], 2000)
        self.assertEqual([entry["type"] for entry in user["ledger"]], ["Admin credit", "Admin debit"])
        self.assertEqual([entry["amount"] for entry in user["ledger"]], [2500, -500])
        transactions = app.public_user_profile(user)["transactions"]
        self.assertCountEqual([entry["amount_ngn"] for entry in transactions], [-500, 2500])

    def test_balance_actions_require_admin_session_and_audit_reference(self):
        status, _, body = self.request(
            "POST", "/api/admin/requests/action",
            urlencode({"request_id": "anything", "action": "credit_verified_deposit", "audit_reference": "REF"}),
        )
        self.assertEqual(status, 401)
        self.assertFalse(json.loads(body)["success"])


if __name__ == "__main__":
    unittest.main()
