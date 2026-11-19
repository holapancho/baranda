# Idea: Salesforce connector with a packaged External Client App

> **Status: exploratory idea.** Not part of the MVP. [`../PLAN.md`](../PLAN.md) is the source of
> truth; if this document conflicts with it, the plan wins. Facts marked *unverified* must be
> checked against current Salesforce and MCP documentation before implementing.

## 1. Context and decision

Distribute an **External Client App (ECA)** inside a **2GP managed package**. Subscriber orgs install
the package; the ECA's consumer key belongs to our packaging org.

**Decision: local MCP.** Each user runs the Salesforce connector on their own machine (stdio
transport), launched by their MCP client (Claude Desktop, Claude Code, a local ADK agent, etc.).

Not chosen: a remote multi-user MCP server (OAuth facade with our own tokens and a server-side
credential broker).

Questions this document answers:

1. How does the user authenticate, and where do tokens live?
2. How is access resolved, and revoked when an admin removes it?
3. What can and cannot be enforced on a machine the user controls?
4. Where should a per-user secret be stored?

## 2. Authentication

The user authorizes their org once with the Salesforce CLI, using the packaged ECA's consumer key:

```bash
sf org login web --client-id <eca_consumer_key> --alias my-org
```

The connector then uses that authorized org:

```json
{
  "mcpServers": {
    "salesforce": {
      "command": "uvx",
      "args": ["baranda-sf", "--target-org", "my-org"]
    }
  }
}
```

- **Identity** = the OS user running the process + the org the CLI authorized. The MCP spec's
  authorization flow applies to HTTP transports; a stdio server takes its credentials from the
  local environment, so there is **no MCP token, no OAuth facade and no JWT** in this design.
- **Tokens** are stored and refreshed by the Salesforce CLI on the user's machine.
  *Unverified:* exact storage location and how the CLI encrypts it (OS keychain vs. key file).
- **ECA callback:** `http://localhost:1717/OauthRedirect`.
- **Prod, sandbox and My Domain:** pass `--instance-url` (`https://login.salesforce.com`,
  `https://test.salesforce.com` or the org's My Domain URL).
- The connector reads `instance_url` from the CLI's org info; never a hard-coded host.

### The consumer secret problem

A secret shipped to every user's machine is not a secret. The ECA works as a **public client with
PKCE**: "Require client secret for web server flow" is turned off.

Salesforce's partner FAQ (section 3) confirms this is acceptable: "Not requiring a client secret
for mobile/public clients is consistent with OAuth 2.0 best practices. Public clients cannot safely
store secrets; PKCE compensates for this."

*Still unverified:* that `sf org login web --client-id` completes with a packaged, secretless ECA
(the CLI may prompt for a secret; leave it empty).

## 3. Mandatory partner security controls

Source: Salesforce's "Connected Apps & ECA Security Controls - Partner App FAQs" (published
2026-10-12) and the ISVforce Guide page it references.

Partner-owned Connected Apps and ECAs in use by **more than two customer production orgs** must
enable four controls. The deadline was **May 11, 2026**; non-compliant integrations may be
suspended.

| Control | What it means for the local connector |
|---|---|
| **PKCE** | Required for any authorization code flow (web server flow included) |
| **Refresh Token Rotation (RTR)** | Every refresh returns a **new** refresh token; reusing an old one returns `invalid_grant` |
| **Refresh Token Idle TTL (30 days)** | A refresh token unused for 30 days expires (sliding window); the user must log in again |
| **Refresh Token IP Allowlist** | Meant for server-held refresh tokens at static IPs; **not applicable** when the token is held by an end-user device. Typically not enforced for custom URI scheme callbacks |

Other rules from the FAQ that apply here:

- **New Connected Apps can no longer be created** (since Spring '26); new integrations must use an ECA.
- **Create the ECA in the DevHub** (usually the PBO) and package it. Never in a scratch org.
- **Locking is permanent.** The "Review Controls" link locks all four controls and they can never be turned off. Lock only after testing.
- Turning the controls on takes effect in subscriber orgs **without a package upgrade**, unless the OAuth logic lives inside managed package code.
- Updating the ECA via the **Metadata API deactivates** RT Idle TTL and RT IP Allowlist, unless they are already locked.
- Turning on Idle TTL **immediately invalidates active access tokens**; clients with a valid refresh token recover automatically.
- **Inadequate flows**: User Agent, Username/Password, Device and Hybrid User Agent flows are not allowed for AppExchange use cases (Username/Password is not available for ECAs at all). Do not use `sf org login device`.
- **One ECA per purpose.** If any callback URL is HTTPS with a valid domain, **all four controls** apply, including the IP Allowlist. Mixing a localhost callback with an HTTPS callback in one ECA would break end-user devices.
- **Shared refresh tokens break under RTR:** if two systems hold the same refresh token, the first to refresh invalidates the other's copy. Salesforce offers no prescriptive solution.
- Packaging the ECA in a **separate, lightweight managed package** is acceptable (each package needs its own security review; a submission fee may apply).
- Subscriber admins can only infer Idle TTL (from the 30-day refresh token expiry setting); they cannot verify the other controls.

### Implications for the local design

- **ECA configuration:** public client (no secret), PKCE on, RTR on, Idle TTL 30 days, **only** the localhost callback (`http://localhost:1717/OauthRedirect`). No HTTPS callbacks in this ECA. *Unverified:* that a localhost callback is exempt from the IP Allowlist the same way a custom URI scheme is; the FAQ only says so explicitly for custom URI schemes (and for canvas apps, "localhost or a custom URI").
- **Any future server-side integration gets its own ECA.**
- **RTR and the CLI:** the connector relies on the Salesforce CLI to store each rotated refresh token. *Unverified:* that the CLI handles RTR correctly.
- **One refresher per token:** the CLI and the connector share the same stored auth. If both refresh at the same time, one copy is invalidated. The connector should never refresh on its own; it should let the CLI do it (or use a lock), and treat `invalid_grant` as "run `sf org login web` again".
- **Idle users:** someone who does not use the connector for 30 days must log in again. The connector returns `kind: "auth"` with the exact login command.
- **Test before locking:** validate PKCE, RTR and Idle TTL with a test ECA in a sandbox, then enable in the real ECA, then lock.

## 4. What can be enforced locally (and what cannot)

The user controls the machine: they can edit the connector's policy files, change the code or
skip the connector and call the API directly with their CLI token. So on a local MCP:

- **baranda's local `guard`** protects the **user from the agent** (prompt injection, mistakes): no
  deploys to prod, read-only queries, no destructive commands. It is still valuable, and it is the
  same `guard` as everywhere else.
- **Company rules** (who may use the app, what actions are allowed, license) must be enforced
  **inside Salesforce**, where the user cannot change them.

Rule of thumb: if a rule must hold even when the user is the attacker, it lives in Salesforce.

## 5. Access: three layers

| Layer | Who decides | Where it runs | What |
|---|---|---|---|
| **1. Can log in** | Salesforce (ECA policy in the subscriber org) | Salesforce | With "Admin approved users are pre-authorized" plus **our package's permission set**, only users enabled by the admin can authorize the app |
| **2. Which data** | Salesforce (the user's permissions) | Salesforce | Profile, permission sets, FLS, sharing. The token cannot see more than the user |
| **3. Which actions** | Our package's Apex + local `guard` | Salesforce (company rules) and local (agent safety) | Custom permission and license checks in Apex; `guard` policy locally |

Because every call uses **the user's** token, a prompt injection that fools the model still cannot
see more than that user already can.

### Route actions through our Apex

To make layer 3 trustworthy, the connector calls **Apex REST endpoints in our package** instead of
running raw SOQL or generic API calls where possible. Each endpoint starts with:

```apex
if (!FeatureManagement.checkPermission('Baranda_Access')) {
    // respond 403
}
```

The same endpoint can check the package license (License Management App or `UserPackageLicense`)
and a **minimum connector version** (see section 8).

## 6. Revocation: the admin removes the permission set

*Unverified:* assume an already-issued access token **keeps working until it expires**, and design
for that. Salesforce checks the ECA policy at login and on refresh, not necessarily on every
standard API call.

| Mechanism | Cuts access | How |
|---|---|---|
| **Custom permission checked in our Apex** | **On the next call** | Every Apex endpoint checks `Baranda_Access`; without it, `403` |
| **Cached check in the connector** | Within the cache TTL (e.g. 5 min) | Before a tool runs, call a tiny `/baranda/access` endpoint; cache the result locally |
| **Admin revokes the app's tokens** in Setup | Immediately | The connector gets 401; refresh fails; login no longer works |
| **Nothing** | When the token expires (session timeout, possibly hours) | Not recommended |

Raw SOQL or standard API calls with a still-valid token bypass our Apex checks, which is why layer
3 should route through Apex. On failure the connector returns `kind: "forbidden"` with a message
like "Your administrator removed access to the app", and the model explains it instead of retrying.

### Integration test (against a test org)

1. Assign the permission set, log in, a tool succeeds.
2. Remove the permission set: on the next call (or within the cache TTL) the tool returns `forbidden`.
3. Revoke the app's tokens in Setup: the tool returns 401; logging in again fails while the permission set is missing.
4. Reassign the permission set: login works again.

Run it on every connector change so a regression cannot leave access open.

## 7. Per-user secrets

The idea of storing a per-user secret in a hierarchy **custom setting** was considered and rejected.

Why not a custom setting (verified through official doc excerpts, not full pages):

- Protected custom settings in a managed package cannot be edited or read via Apex by the subscriber org. Outside a package they behave as public, and public custom settings are readable by all profiles, **including the guest user**.
- Salesforce advises against storing secrets in custom settings, **even protected ones**, and recommends Named Credentials or encrypted fields.
- Since Spring '20, users without "Customize Application" cannot read custom settings outside system context; access is granted with "View All Custom Settings".
- Values are stored in plain text, and admins with "View All Data" / "Modify All Data" can read them.
- *Unverified:* custom setting data being copied to sandboxes on refresh.

Options, depending on who uses the secret:

| Who uses the secret? | Where to store it |
|---|---|
| **Salesforce** makes the call to the external system | **Named Credential + External Credential with a per-user principal.** Native, encrypted, not readable via SOQL, Apex or APIs (only used at callout time), packageable in 2GP (named credential, external credential and a permission set granting the principal; the principal is populated after install via the Connect API). Callouts are blocked if the user has no permission on the principal |
| **Our package's Apex** needs it | Protected custom setting or custom metadata. Common ISV practice, but not encrypted |
| **The local connector** needs it | The **OS keychain** (e.g. via the Python `keyring` library), never in plain files or environment variables committed anywhere |

Preferred pattern when the secret is for a third party: **the secret never leaves Salesforce**.

```
Local connector ──(user's token)──► Our package's Apex REST
                                         │ callout with the Named Credential
                                         │ (per-user credential)
                                         ▼
                                    External system
```

### How the user enters the secret

**Never through the chat or the agent.** The model would see it, and it would end up in the session
history and possibly in traces. Use a separate path:

- Inside Salesforce: an LWC in our package, or the user's external credentials page if using Named Credentials (*unverified:* exact UI).
- Locally: a CLI command that prompts without echo, e.g. `baranda-sf secrets set <name>`, writing to the OS keychain.
- If the connector must ask for it during a session, use **URL-mode elicitation** pointing to one of the above. The MCP spec states servers must not use form-mode elicitation for sensitive information (passwords, API keys, tokens); URL mode was introduced in the 2025-11-25 spec.

## 8. Distribution and updates

- Ship the connector as a Python package (`uvx baranda-sf`) or a Docker image.
- A security fix only helps once users update. Enforce a **minimum version**: the `/baranda/access`
  endpoint returns the minimum supported connector version, and older connectors refuse to run
  with a clear message.

## 9. Observability and audit

- Local traces and audit are useful for the user but **not trustworthy for the company** (the user
  can disable or edit them).
- Trusted audit lives in Salesforce: our Apex endpoints record actions (custom object or platform
  event), plus Salesforce's own logs (Setup Audit Trail, API usage, Event Monitoring if licensed).

## 10. Risks

- **Mandatory controls (section 3):** once the app is in more than two customer production orgs, PKCE, RTR, Idle TTL and RT IP Allowlist are required, and locking them is irreversible. A mistake in the ECA configuration (for example, adding an HTTPS callback) can break every user's login.
- **RTR with a shared CLI token store:** concurrent refreshes by the CLI and the connector can invalidate each other (section 3).
- **`OAUTH_AUTHORIZATION_BLOCKED` ("cross-org OAuth flows are not supported").** A community report on Trailblazer describes this error when authorizing from a subscriber org with a packaged ECA, with no confirmed fix. **Test early with a real subscriber org.**
- Packaged ECAs keep their global OAuth settings (consumer credentials) in the owning org, shared by all installs; that org **must remain available**. Subscribers can only edit policies, not settings.
- Token behavior after permission set removal is unverified (section 6).
- Anything enforced only locally can be bypassed by the user (section 4).

## 11. Fit with baranda

- The connector is a normal baranda connector: `server.py`, `policy.yaml`, `skills/`, using `guard`.
- No auth server and no credential broker: the Salesforce CLI provides the user's credentials.
- `guard` protects the user from the agent; Salesforce (our package's Apex, permission sets, ECA policies) enforces company rules.

## 12. Open questions

- Does `sf org login web --client-id` work with a packaged, secretless ECA with PKCE?
- Does the Salesforce CLI store rotated refresh tokens correctly under RTR, and is it safe against concurrent refreshes?
- Is a `localhost` callback exempt from the RT IP Allowlist, like a custom URI scheme?
- Exact behavior of existing access tokens after permission set removal.
- Where and how the Salesforce CLI stores tokens on each OS.
- Whether custom setting data is copied to each sandbox type.
- Exact UI for users to enter per-user external credentials.
- License check: License Management App vs. `UserPackageLicense`.

## 13. References

Official, provided as full text by the user:

- Salesforce Knowledge: "Salesforce Connected Apps & ECA Security Controls - Partner App FAQs" (published 2026-10-12)
- [ISVforce Guide: Secure Your Connected Apps and External Client Apps](https://developer.salesforce.com/docs/atlas.en-us.packagingGuide.meta/packagingGuide/secure_code_ac_eca.htm)
- [CA and ECA AppExchange Security Review Submission Guidance](https://help.salesforce.com/s/articleView?id=005318040&type=1&language=en_US)
- [Enforce One-Time-Use Refresh Tokens with Refresh Token Rotation (RTR)](https://help.salesforce.com/s/articleView?id=005316711&type=1&language=en_US)
- [Enabling PKCE for OAuth for External Client and Connected Apps](https://help.salesforce.com/s/articleView?id=005316703&type=1&language=en_US)
- [What to Do When OAuth Security Settings Are Locked](https://help.salesforce.com/s/articleView?id=005433465&type=1&language=en_US)
- [OAuth 2.0 Authorization Errors](https://help.salesforce.com/s/articleView?id=xcloud.remoteaccess_oauth_flow_errors.htm&type=5&language=en_US)

Found through search excerpts; the pages themselves could not be opened from the design session
(network policy), so re-read them before implementing.

- [Apex Developer Guide: Custom Settings](https://developer.salesforce.com/docs/atlas.en-us.apexcode.meta/apexcode/apex_customsettings.htm)
- [Secure Custom Metadata Types and Custom Settings in Winter '20](https://admin.salesforce.com/blog/2019/secure-custom-metadata-types-and-custom-settings-in-winter-20)
- [Create Custom Settings (Salesforce Help)](https://help.salesforce.com/apex/HTViewHelpDoc?id=cs_about.htm)
- [Named Credentials Glossary](https://developer.salesforce.com/docs/platform/named-credentials/references/named-credentials-reference/nc-glossary.html)
- [Package Named Credentials](https://developer.salesforce.com/docs/platform/named-credentials/guide/nc-package-credentials.html)
- [Named Credentials Packaging Guide](https://developer.salesforce.com/docs/platform/named-credentials/guide/nc-packaging-dev-guide.html)
- [Trailhead: Package and Distribute External Client Apps](https://trailhead.salesforce.com/content/learn/modules/external-client-app-basics/package-and-distribute-external-client-apps)
- [Trailblazer Community: OAUTH_AUTHORIZATION_BLOCKED](https://trailhead.salesforce.com/trailblazer-community/feed/0D5KX00000pNxl60AC)
- [MCP spec 2025-11-25: Elicitation](https://modelcontextprotocol.io/specification/2025-11-25/client/elicitation)
- [MCP spec draft: Elicitation](https://modelcontextprotocol.io/specification/draft/client/elicitation)
- [SEP-1036: URL mode elicitation](https://modelcontextprotocol.io/seps/1036-url-mode-elicitation-for-secure-out-of-band-intera)
