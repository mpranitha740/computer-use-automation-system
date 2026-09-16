"""
Mock "legacy" core-banking admin console.

Deliberately styled like a real-world legacy enterprise app:
- server-rendered HTML, no client-side framework
- table-based layout, no CSS classes chosen for automation, no data-testid attributes
- plain <form> POSTs, full page reloads
- a short-lived session cookie to simulate session/timeout expiry
- deliberate business-outcome branches: member not found, locked/permission-denied
  member, validation errors on the sub-account form

This is a stand-in for the real thing, built so the agent has no clean DOM /
test-id shortcuts and has to rely on visible text, table structure and roles —
the same constraint a real legacy bank admin console imposes.
"""
import time
import uuid
from flask import Flask, request, redirect, url_for, session, make_response

app = Flask(__name__)
app.secret_key = "dev-only-not-a-real-secret"

# Session cookies expire fast so a replay can be made to hit a real timeout.
SESSION_TTL_SECONDS = 90

MEMBERS = {
    "12345": {"name": "Alice Johnson", "status": "active", "savings_balance": "4,215.30",
              "accounts": [("SAV-1001", "Savings", "4,215.30"), ("CHK-2001", "Checking", "812.44")]},
    "67890": {"name": "Robert Chen", "status": "active", "savings_balance": "150.00",
              "accounts": [("SAV-1002", "Savings", "150.00")]},
    "99999": {"name": "Locked Member", "status": "locked", "savings_balance": "0.00",
              "accounts": []},
}

# in-memory store of opened sub-accounts, keyed by member id
NEW_ACCOUNTS = {}

PAGE_HEAD = """<html><head><title>{title}</title></head><body>
<table border="0" cellpadding="4" cellspacing="0" width="100%">
<tr><td bgcolor="#003366"><font color="white"><b>&nbsp;CoreBank Admin Console</b></font></td></tr>
</table>
<hr>
"""
PAGE_TAIL = "</body></html>"


def require_session():
    login_at = session.get("login_at")
    if not login_at or (time.time() - login_at) > SESSION_TTL_SECONDS:
        return False
    return True


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username", "")
        pwd = request.form.get("password", "")
        if user == "operator" and pwd == "demo-pass":
            session["login_at"] = time.time()
            session["user"] = user
            return redirect(url_for("search"))
        body = f"""{PAGE_HEAD.format(title='Login')}
        <p><font color="red">Invalid credentials.</font></p>
        <form method="post">
        <table>
        <tr><td>Username</td><td><input type="text" name="username"></td></tr>
        <tr><td>Password</td><td><input type="password" name="password"></td></tr>
        </table>
        <input type="submit" value="Log In">
        </form>{PAGE_TAIL}"""
        return body
    body = f"""{PAGE_HEAD.format(title='Login')}
    <form method="post">
    <table>
    <tr><td>Username</td><td><input type="text" name="username"></td></tr>
    <tr><td>Password</td><td><input type="password" name="password"></td></tr>
    </table>
    <input type="submit" value="Log In">
    </form>{PAGE_TAIL}"""
    return body


@app.route("/members/search", methods=["GET", "POST"])
def search():
    if not require_session():
        return redirect(url_for("login"))
    result_html = ""
    if request.method == "POST":
        member_id = request.form.get("member_id", "").strip()
        return redirect(url_for("member_detail", member_id=member_id))
    body = f"""{PAGE_HEAD.format(title='Member Search')}
    <p>Logged in as {session.get('user')}</p>
    <form method="post">
    <table>
    <tr><td>Member ID</td><td><input type="text" name="member_id"></td></tr>
    </table>
    <input type="submit" value="Search">
    </form>
    {result_html}{PAGE_TAIL}"""
    return body


@app.route("/members/<member_id>")
def member_detail(member_id):
    if not require_session():
        return redirect(url_for("login"))
    m = MEMBERS.get(member_id)
    if not m:
        body = f"""{PAGE_HEAD.format(title='Member Detail')}
        <p><b>No member found for ID {member_id}.</b></p>
        <p><a href="{url_for('search')}">Back to search</a></p>{PAGE_TAIL}"""
        return body
    if m["status"] == "locked":
        body = f"""{PAGE_HEAD.format(title='Member Detail')}
        <p><b>Access denied: member {member_id} record is locked / restricted.</b></p>
        <p>Contact a supervisor to unlock this record before proceeding.</p>
        <p><a href="{url_for('search')}">Back to search</a></p>{PAGE_TAIL}"""
        return body
    rows = "".join(
        f"<tr><td>{acc[0]}</td><td>{acc[1]}</td><td>{acc[2]}</td></tr>" for acc in m["accounts"]
    )
    new_acc = NEW_ACCOUNTS.get(member_id)
    new_acc_row = ""
    if new_acc:
        new_acc_row = f"<tr><td>{new_acc['account_number']}</td><td>{new_acc['account_type']}</td><td>{new_acc['deposit']}</td></tr>"
    body = f"""{PAGE_HEAD.format(title='Member Detail')}
    <h3>Member {member_id}: {m['name']}</h3>
    <p>Status: {m['status']}</p>
    <p>Current Savings Balance: <b>${m['savings_balance']}</b></p>
    <table border="1" cellpadding="4">
    <tr><td><b>Account #</b></td><td><b>Type</b></td><td><b>Balance</b></td></tr>
    {rows}{new_acc_row}
    </table>
    <p><a href="{url_for('new_sub_account', member_id=member_id)}">Open new sub-account</a></p>
    <p><a href="{url_for('search')}">Back to search</a></p>{PAGE_TAIL}"""
    return body


@app.route("/members/<member_id>/sub-accounts/new", methods=["GET", "POST"])
def new_sub_account(member_id):
    if not require_session():
        return redirect(url_for("login"))
    m = MEMBERS.get(member_id)
    if not m:
        return redirect(url_for("member_detail", member_id=member_id))
    error = ""
    if request.method == "POST":
        account_type = request.form.get("account_type", "")
        deposit = request.form.get("deposit", "")
        try:
            deposit_val = float(deposit)
            if deposit_val <= 0:
                raise ValueError()
        except ValueError:
            error = "<p><font color=\"red\">Validation error: initial deposit must be a positive number.</font></p>"
        else:
            session["pending_account_type"] = account_type
            session["pending_deposit"] = deposit
            return redirect(url_for("confirm_sub_account", member_id=member_id))
    body = f"""{PAGE_HEAD.format(title='Open Sub-Account')}
    <h3>Open New Sub-Account for {member_id}: {m['name']}</h3>
    {error}
    <form method="post">
    <table>
    <tr><td>Account Type</td><td>
      <select name="account_type">
        <option value="Savings">Savings</option>
        <option value="Checking">Checking</option>
        <option value="CD">Certificate of Deposit</option>
      </select>
    </td></tr>
    <tr><td>Initial Deposit ($)</td><td><input type="text" name="deposit"></td></tr>
    </table>
    <input type="submit" value="Continue">
    </form>{PAGE_TAIL}"""
    return body


@app.route("/members/<member_id>/sub-accounts/new/confirm", methods=["GET", "POST"])
def confirm_sub_account(member_id):
    if not require_session():
        return redirect(url_for("login"))
    account_type = session.get("pending_account_type")
    deposit = session.get("pending_deposit")
    if not account_type or not deposit:
        return redirect(url_for("new_sub_account", member_id=member_id))
    if request.method == "POST":
        # irreversible: actually opens the account
        acct_num = f"{account_type[:3].upper()}-{uuid.uuid4().hex[:6].upper()}"
        NEW_ACCOUNTS[member_id] = {
            "account_number": acct_num, "account_type": account_type, "deposit": deposit,
        }
        session.pop("pending_account_type", None)
        session.pop("pending_deposit", None)
        body = f"""{PAGE_HEAD.format(title='Sub-Account Opened')}
        <h3>Sub-account opened successfully</h3>
        <p>Account Number: <b>{acct_num}</b></p>
        <p>Type: {account_type}, Initial Deposit: ${deposit}</p>
        <p><a href="{url_for('member_detail', member_id=member_id)}">Back to member</a></p>{PAGE_TAIL}"""
        return body
    body = f"""{PAGE_HEAD.format(title='Confirm Sub-Account')}
    <h3>Confirm New Sub-Account for {member_id}</h3>
    <table border="1" cellpadding="4">
    <tr><td>Account Type</td><td>{account_type}</td></tr>
    <tr><td>Initial Deposit</td><td>${deposit}</td></tr>
    </table>
    <p><b>This action is irreversible once confirmed.</b></p>
    <form method="post">
    <input type="submit" value="Confirm &amp; Open Account">
    </form>
    <p><a href="{url_for('new_sub_account', member_id=member_id)}">Back / edit</a></p>{PAGE_TAIL}"""
    return body


@app.route("/")
def index():
    return redirect(url_for("search"))


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5055, debug=False)
