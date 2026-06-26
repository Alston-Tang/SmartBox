#!/usr/bin/env python3
"""Apply initial SmartBox configuration to Stalwart via the recovery JMAP API."""

import base64
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path


BOOTSTRAP_VERSION = "plaintext-v6"
MARKER = Path("/var/lib/stalwart/.smartbox-initialized-v6")
ROLES_MARKER = Path("/var/lib/stalwart/.smartbox-roles-v1")
RELAY_MARKER = Path("/var/lib/stalwart/.smartbox-relay-v1")
JMAP_URL = os.environ.get("STALWART_JMAP_URL", "http://127.0.0.1:8080/jmap")
ADMIN_USER = os.environ.get("STALWART_ADMIN_USER", "admin")
ADMIN_PASSWORD = os.environ.get("STALWART_ADMIN_PASSWORD", "smartbox-admin")
recovery_admin = os.environ.get("STALWART_RECOVERY_ADMIN")
if recovery_admin and ":" in recovery_admin:
    ADMIN_USER, _, ADMIN_PASSWORD = recovery_admin.partition(":")
MAIL_DOMAIN = os.environ.get("MAIL_DOMAIN", "mail.thm64.com")
MAIL_USER = os.environ.get("MAIL_USER", "default")
MAIL_PASSWORD = os.environ.get("MAIL_PASSWORD", "smartbox-dev")
MAIL_HOSTNAME = os.environ.get("MAIL_HOSTNAME", MAIL_DOMAIN)

# Outbound SMTP relay (smarthost). When SMTP_RELAY_HOST is set, mail to external
# domains is forwarded through this relay instead of direct MX delivery.
RELAY_HOST = os.environ.get("SMTP_RELAY_HOST", "").strip()
RELAY_PORT = int(os.environ.get("SMTP_RELAY_PORT", "465") or "465")
RELAY_USERNAME = os.environ.get("SMTP_RELAY_USERNAME", "").strip()
RELAY_PASSWORD_ENV = "SMTP_RELAY_PASSWORD"
RELAY_IMPLICIT_TLS = os.environ.get("SMTP_RELAY_IMPLICIT_TLS", "true").lower() == "true"
RELAY_ALLOW_INVALID_CERTS = os.environ.get("SMTP_RELAY_ALLOW_INVALID_CERTS", "false").lower() == "true"
RELAY_ROUTE_NAME = "smartbox-relay"


def jmap(method_calls):
    auth = base64.b64encode(f"{ADMIN_USER}:{ADMIN_PASSWORD}".encode()).decode()
    body = json.dumps(
        {"using": ["urn:ietf:params:jmap:core", "urn:stalwart:jmap"], "methodCalls": method_calls}
    ).encode()
    request = urllib.request.Request(
        JMAP_URL,
        data=body,
        headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"},
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read())


def wait_for_jmap(timeout_seconds=120):
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        try:
            jmap([["x:Domain/query", {}, "ping"]])
            return
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 503):
                time.sleep(2)
                continue
            if exc.code == 401:
                raise RuntimeError(
                    "Stalwart JMAP rejected recovery admin credentials; "
                    "set STALWART_RECOVERY_ADMIN to match STALWART_ADMIN_USER/PASSWORD"
                ) from exc
            raise
        except urllib.error.URLError:
            time.sleep(2)
    raise RuntimeError("Stalwart JMAP API did not become ready in time")


def domain_spec():
    return {
        "name": MAIL_DOMAIN,
        "aliases": {},
        "certificateManagement": {"@type": "Manual"},
        "dkimManagement": {"@type": "Automatic"},
        "dnsManagement": {"@type": "Manual"},
        "subAddressing": {"@type": "Enabled"},
    }


def ensure_domain():
    response = jmap([["x:Domain/query", {"filter": {"name": MAIL_DOMAIN}}, "q"]])
    ids = response["methodResponses"][0][1].get("ids") or []
    if ids:
        jmap(
            [
                [
                    "x:Domain/set",
                    {"update": {ids[0]: domain_spec()}},
                    "d1",
                ]
            ]
        )
        return ids[0]

    created = jmap(
        [
            [
                "x:Domain/set",
                {"create": {"dom-mail": domain_spec()}},
                "c1",
            ]
        ]
    )
    result = created["methodResponses"][0][1]
    if result.get("created"):
        return result["created"]["dom-mail"]["id"]
    raise RuntimeError(f"Failed to create domain: {result}")


def ensure_listener(name, protocol, port, use_tls=False, tls_implicit=False):
    response = jmap([["x:NetworkListener/query", {"filter": {"name": name}}, "q"]])
    ids = response["methodResponses"][0][1].get("ids") or []
    if ids:
        return ids[0]

    listener = {
        "name": name,
        "protocol": protocol,
        "bind": {f"[::]:{port}": True},
        "useTls": use_tls,
        "tlsImplicit": tls_implicit,
    }
    created = jmap([["x:NetworkListener/set", {"create": {name: listener}}, "c1"]])
    result = created["methodResponses"][0][1]
    if result.get("created"):
        return result["created"][name]["id"]
    raise RuntimeError(f"Failed to create listener {name}: {result}")


def ensure_dev_auth_policy():
    jmap(
        [
            [
                "x:Authentication/set",
                {
                    "update": {
                        "singleton": {
                            "passwordMinLength": 1,
                            "passwordMinStrength": "zero",
                        }
                    }
                },
                "a1",
            ]
        ]
    )


def ensure_imap_plaintext_auth():
    jmap(
        [
            [
                "x:Imap/set",
                {"update": {"singleton": {"allowPlainTextAuth": True}}},
                "i1",
            ]
        ]
    )


def account_body(domain_id, credentials=None):
    body = {
        "@type": "User",
        "name": MAIL_USER,
        "domainId": domain_id,
        "roles": {"@type": "User"},
        "permissions": {"@type": "Inherit"},
        "encryptionAtRest": {"@type": "Disabled"},
        "aliases": {},
        "memberGroupIds": {},
        "quotas": {},
    }
    body["credentials"] = credentials if credentials is not None else {}
    return body


def destroy_mailbox_accounts(domain_id):
    response = jmap(
        [
            [
                "x:Account/query",
                {"filter": {"name": MAIL_USER, "domainId": domain_id}},
                "q",
            ]
        ]
    )
    ids = response["methodResponses"][0][1].get("ids") or []
    if ids:
        jmap([["x:Account/set", {"destroy": ids}, "d1"]])


def create_mailbox_account(domain_id):
    password_credential = {
        "@type": "Password",
        "secret": MAIL_PASSWORD,
    }
    create_variants = (
        [password_credential],
        {},
    )

    last_result = None
    account_id = None
    for credentials in create_variants:
        created = jmap(
            [
                [
                    "x:Account/set",
                    {
                        "create": {
                            "user-default": account_body(domain_id, credentials),
                        }
                    },
                    "c1",
                ]
            ]
        )
        result = created["methodResponses"][0][1]
        last_result = result
        if result.get("created"):
            account_id = result["created"]["user-default"]["id"]
            if credentials:
                return account_id

    if account_id is None:
        raise RuntimeError(f"Failed to create mailbox account: {last_result}")

    updated = jmap(
        [
            [
                "x:Account/set",
                {
                    "update": {
                        account_id: {"credentials/0/secret": MAIL_PASSWORD}
                    }
                },
                "u1",
            ]
        ]
    )
    result = updated["methodResponses"][0][1]
    if account_id not in (result.get("updated") or {}):
        raise RuntimeError(f"Failed to set mailbox password: {result}")
    return account_id


def verify_account_credentials(account_id):
    fetched = jmap(
        [
            [
                "x:Account/get",
                {"ids": [account_id], "properties": ["id", "name", "credentials"]},
                "g1",
            ]
        ]
    )
    account = (fetched["methodResponses"][0][1].get("list") or [{}])[0]
    credentials = account.get("credentials") or {}
    if not credentials:
        raise RuntimeError(
            f"Mailbox account {account_id} has no credentials after bootstrap"
        )
    print(
        f"Mailbox account ready: {account.get('name')} "
        f"({len(credentials)} credential(s))",
        flush=True,
    )


def wait_for_roles(timeout_seconds=60):
    """Built-in roles are only seeded on a normal (non-recovery) boot."""
    deadline = time.time() + timeout_seconds
    while time.time() < deadline:
        response = jmap([["x:Role/query", {}, "q"]])
        role_ids = response["methodResponses"][0][1].get("ids") or []
        if role_ids:
            return role_ids
        time.sleep(2)
    raise RuntimeError("Built-in roles never appeared; cannot set default roles")


def ensure_default_roles():
    """Point the Authentication singleton at the built-in roles.

    A recovery-only bootstrap never runs the setup wizard, so
    `defaultUserRoleIds`/`defaultAdminRoleIds` stay empty. As a result the
    built-in `User` role variant on every account resolves to *no* roles,
    which means accounts lack the `authenticate` permission and the server
    closes IMAP/SMTP sessions right after a successful password check
    (observed as `socket error: EOF`). The built-in roles only exist once the
    server has booted normally, so this step runs against the running normal
    server rather than during the recovery-mode bootstrap.
    """
    role_ids = wait_for_roles()

    fetched = jmap(
        [["x:Role/get", {"ids": role_ids, "properties": ["id", "description"]}, "g"]]
    )
    roles_by_desc = {
        r.get("description"): r.get("id")
        for r in fetched["methodResponses"][0][1].get("list", [])
    }
    user_role_id = roles_by_desc.get("User")
    admin_role_id = roles_by_desc.get("System Administrator")
    if not user_role_id:
        raise RuntimeError(f"Built-in 'User' role not found among {roles_by_desc}")

    update = {"defaultUserRoleIds": {user_role_id: True}}
    if admin_role_id:
        update["defaultAdminRoleIds"] = {admin_role_id: True}

    result = jmap(
        [["x:Authentication/set", {"update": {"singleton": update}}, "dr"]]
    )["methodResponses"][0][1]
    if "singleton" not in (result.get("updated") or {}):
        raise RuntimeError(f"Failed to set default roles: {result}")
    print(f"Default roles set (user={user_role_id})", flush=True)


def ensure_smtp_submission_policy():
    """Enable authenticated submission over plaintext SMTP and relax From checks.

    Internal-network-only service with no TLS. By default Stalwart only offers SMTP
    AUTH on TLS-protected, non-25 ports and refuses to relay to external domains
    from unauthenticated sessions ("550 5.1.2 Relay not allowed"). SASL PLAIN/LOGIN
    is enabled on port 25 so Roundcube and mail clients can authenticate.

    From-address checks are disabled for authenticated submission: any MAIL FROM is
    accepted and relayed once the session is authenticated. Unauthenticated inbound
    mail can still only be delivered to local domains.
    """
    auth_update = {
        "saslMechanisms": {"else": "[plain, login]"},
        "mustMatchSender": {"else": "false"},
    }
    mail_update = {
        "isSenderAllowed": {"else": "true"},
    }
    rcpt_update = {
        "allowRelaying": {"else": "!is_empty(authenticated_as)"},
    }
    ehlo_update = {
        # Roundcube/docker clients often EHLO with a short container hostname.
        "rejectNonFqdn": {"else": "false"},
    }
    response = jmap(
        [
            ["x:MtaStageAuth/set", {"update": {"singleton": auth_update}}, "ms"],
            ["x:MtaStageMail/set", {"update": {"singleton": mail_update}}, "mm"],
            ["x:MtaStageRcpt/set", {"update": {"singleton": rcpt_update}}, "mr"],
            ["x:MtaStageEhlo/set", {"update": {"singleton": ehlo_update}}, "me"],
        ]
    )
    for idx, label in enumerate(
        ("MtaStageAuth", "MtaStageMail", "MtaStageRcpt", "MtaStageEhlo")
    ):
        result = response["methodResponses"][idx][1]
        if "singleton" not in (result.get("updated") or {}):
            raise RuntimeError(f"Failed to set SMTP {label} policy: {result}")
    print(
        "SMTP policy: AUTH on plaintext ports; From/EHLO checks relaxed; "
        "relay allowed when authenticated",
        flush=True,
    )


def ensure_relay():
    """Configure an outbound smarthost relay for external recipients.

    Creates (or updates) an MtaRoute of type Relay and points the
    MtaOutboundStrategy `route` expression at it so that mail to non-local
    domains is forwarded through the relay instead of via direct MX delivery.
    Local-domain mail continues to be delivered locally.

    The relay password is read by Stalwart at runtime from the
    SMTP_RELAY_PASSWORD environment variable (loaded into the container via
    env_file), so the secret is never written to the database.
    """
    if not RELAY_HOST:
        print("No SMTP_RELAY_HOST set; skipping relay configuration", flush=True)
        return

    if RELAY_USERNAME and os.environ.get(RELAY_PASSWORD_ENV):
        auth_secret = {"@type": "EnvironmentVariable", "variableName": RELAY_PASSWORD_ENV}
    else:
        auth_secret = {"@type": "None"}

    # `name` is read-only after creation, so it is only sent on create.
    route_body = {
        "@type": "Relay",
        "address": RELAY_HOST,
        "port": RELAY_PORT,
        "protocol": "smtp",
        "implicitTls": RELAY_IMPLICIT_TLS,
        "allowInvalidCerts": RELAY_ALLOW_INVALID_CERTS,
        "authSecret": auth_secret,
        "description": "SmartBox outbound relay",
    }
    if RELAY_USERNAME:
        route_body["authUsername"] = RELAY_USERNAME

    existing = jmap(
        [["x:MtaRoute/query", {"filter": {"name": RELAY_ROUTE_NAME}}, "q"]]
    )["methodResponses"][0][1].get("ids") or []

    if existing:
        route_id = existing[0]
        result = jmap(
            [["x:MtaRoute/set", {"update": {route_id: route_body}}, "u"]]
        )["methodResponses"][0][1]
        if route_id not in (result.get("updated") or {}):
            raise RuntimeError(f"Failed to update relay route: {result}")
    else:
        create_body = {"name": RELAY_ROUTE_NAME, **route_body}
        created = jmap(
            [["x:MtaRoute/set", {"create": {RELAY_ROUTE_NAME: create_body}}, "c"]]
        )["methodResponses"][0][1]
        if not created.get("created"):
            raise RuntimeError(f"Failed to create relay route: {created}")
        route_id = created["created"][RELAY_ROUTE_NAME]["id"]

    # Discover the route's actual name (read-only, assigned by the server) so the
    # strategy expression references the correct identifier.
    fetched = jmap(
        [["x:MtaRoute/get", {"ids": [route_id], "properties": ["id", "name"]}, "g"]]
    )["methodResponses"][0][1].get("list") or [{}]
    route_name = fetched[0].get("name") or RELAY_ROUTE_NAME

    # `match` is stored as a map keyed by string indices, not a JSON array.
    route_expr = {
        "match": {"0": {"if": "is_local_domain(rcpt_domain)", "then": "'local'"}},
        "else": f"'{route_name}'",
    }
    result = jmap(
        [
            [
                "x:MtaOutboundStrategy/set",
                {"update": {"singleton": {"route": route_expr}}},
                "s",
            ]
        ]
    )["methodResponses"][0][1]
    if "singleton" not in (result.get("updated") or {}):
        raise RuntimeError(f"Failed to update outbound strategy: {result}")
    print(
        f"Outbound relay configured: {RELAY_HOST}:{RELAY_PORT} "
        f"(route '{route_name}', implicitTls={RELAY_IMPLICIT_TLS})",
        flush=True,
    )


def ensure_mailbox(domain_id):
    destroy_mailbox_accounts(domain_id)
    account_id = create_mailbox_account(domain_id)
    verify_account_credentials(account_id)

    jmap(
        [
            [
                "x:SystemSettings/set",
                {
                    "update": {
                        "singleton": {
                            "defaultHostname": MAIL_HOSTNAME,
                            "defaultDomainId": domain_id,
                        }
                    }
                },
                "s1",
            ]
        ]
    )


def ensure_console_tracer():
    stdout_tracer = {
        "@type": "Stdout",
        "level": "trace",
        "enable": True,
        "ansi": False,
        "buffered": False,
    }

    response = jmap([["x:Tracer/query", {}, "q"]])
    ids = response["methodResponses"][0][1].get("ids") or []
    if ids:
        updated = jmap(
            [["x:Tracer/set", {"update": {ids[0]: stdout_tracer}}, "tu"]]
        )
        result = updated["methodResponses"][0][1]
        if ids[0] in (result.get("updated") or {}):
            return

    created = jmap(
        [["x:Tracer/set", {"create": {"trace-stdout": stdout_tracer}}, "tc"]]
    )
    result = created["methodResponses"][0][1]
    if not result.get("created"):
        print(f"Warning: could not configure Stdout tracer: {result}", flush=True)


def configure_smartbox():
    ensure_dev_auth_policy()
    ensure_console_tracer()
    time.sleep(2)
    domain_id = ensure_domain()
    ensure_listener("http", "http", 8080, use_tls=False)
    ensure_listener("smtp", "smtp", 25, use_tls=False)
    ensure_listener("imap", "imap", 143, use_tls=False)
    ensure_imap_plaintext_auth()
    ensure_mailbox(domain_id)
    MARKER.parent.mkdir(parents=True, exist_ok=True)
    MARKER.write_text("ok\n", encoding="utf-8")
    print("Stalwart bootstrap complete", flush=True)


def main():
    phase = os.environ.get("SMARTBOX_PHASE", "recovery")
    print(f"SmartBox bootstrap {BOOTSTRAP_VERSION} (phase={phase})", flush=True)
    wait_for_jmap()

    if phase == "policy":
        ensure_smtp_submission_policy()
        return 0

    if phase == "roles":
        if not ROLES_MARKER.is_file():
            ensure_default_roles()
            ROLES_MARKER.parent.mkdir(parents=True, exist_ok=True)
            ROLES_MARKER.write_text("ok\n", encoding="utf-8")
        else:
            print("Default roles already configured", flush=True)

        ensure_smtp_submission_policy()

        if not RELAY_MARKER.is_file():
            ensure_relay()
            RELAY_MARKER.parent.mkdir(parents=True, exist_ok=True)
            RELAY_MARKER.write_text("ok\n", encoding="utf-8")
        else:
            print("Outbound relay already configured", flush=True)
        return 0

    if MARKER.is_file():
        print("Stalwart already initialized", flush=True)
        return 0

    configure_smartbox()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"Stalwart bootstrap failed: {exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
