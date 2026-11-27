# Product Requirements Document - Marvin (TSP-NG)

> **Version:** 3.0
> **Status:** Preparing for first production deployment
> **License:** AGPL v3
> **Last updated:** 2026-11-25

---

## Table of Contents

1. [Overview & Background](#1-overview--background)
2. [Goals & Non-Goals](#2-goals--non-goals)
3. [User Groups & Personas](#3-user-groups--personas)
4. [System Architecture](#4-system-architecture)
5. [Data Model](#5-data-model)
6. [Request Lifecycle - State Machine](#6-request-lifecycle---state-machine)
7. [Feature Requirements](#7-feature-requirements)
8. [Route Reference](#8-route-reference)
9. [Authentication & Authorization](#9-authentication--authorization)
10. [Payment Integration - Wise](#10-payment-integration---wise)
11. [Notifications](#11-notifications)
12. [Document Management](#12-document-management)
13. [Reporting, Search & Audit](#13-reporting-search--audit)
14. [Known Gaps & Limitations](#14-known-gaps--limitations)
15. [Roadmap](#15-roadmap)

---

## 1. Overview & Background

### 1.1 What is Marvin?

**Marvin** is a Django web application for managing travel sponsorship requests and expense reimbursements for free and open-source software (FOSS) organizations. It is the next-generation implementation of the Travel Support Program (TSP) run by the Geeko Foundation for openSUSE, and is designed for reuse by organizations such as GNOME (Conference Travel Subsidy Program) and KDE e.V. (Travel Cost Reimbursement).

Marvin replaces an earlier Node.js/Express prototype and the legacy Ruby on Rails application, [openSUSE/travel-support-program](https://github.com/openSUSE/travel-support-program).

### 1.2 Problem Statement

FOSS organizations routinely sponsor contributors to attend conferences, give talks, and represent the community. Manual processes lead to:

- **Opaque status** - requesters don't know where their request is in the pipeline
- **Slow turnaround** - manual handoffs between TSP committee, requester, and finance
- **Weak audit trail** - state changes and approvals are not systematically logged
- **Payment friction** - international transfers require manual SWIFT/IBAN entry and tracking
- **Reporting gaps** - no aggregate view of spend per event, per country, or over time

Marvin addresses these with a structured request lifecycle, role-based dashboards, event budgets, Wise payment integration, email and browser push notifications, an append-only audit log, and audit fields on all domain models.

### 1.3 Scope

This PRD describes **Marvin as implemented today** in this repository, plus known gaps and planned improvements.

---

## 2. Goals & Non-Goals

### 2.1 Goals

| # | Goal | Status |
|---|---|---|
| G1 | **End-to-end request lifecycle** - submission through approval, acceptance, actual costs, documents and payment | Done |
| G2 | **Group-based dashboards** - each group sees actionable, scoped data | Done: requester, committee, organiser and finance sections |
| G3 | **Wise integration** - reduce international payment friction | Done: recipient verification, single and batch transfers, status tracking, webhooks |
| G4 | **Audit trail** - every state change and action recorded | Done: audit fields on domain models plus an append-only audit log |
| G5 | **Financial reporting** - spend by event, status and date range | Done: filterable reports with CSV export, event budgets |
| G6 | **Self-hostable** - small FOSS orgs can run their own instance | Done: SQLite for development, PostgreSQL for production |
| G7 | **Portable** - not hardcoded to openSUSE | Done: branding through environment variables, pluggable payments |
| G8 | **Privacy-conscious banking** - bank account numbers never stored locally | Done: only the Wise recipient ID and metadata are kept |
| G9 | **Timely alerts** - users hear about activity without checking | Done: email and opt-in browser push, each kind can be muted |

### 2.2 Non-Goals (current release)

| # | Non-Goal | Rationale |
|---|---|---|
| NG1 | Multi-tenant SaaS | Each org runs its own instance |
| NG2 | OAuth/SSO integration | Username/password with two-step sign-in; SSO is roadmap |
| NG3 | Mobile-native app | Responsive web is sufficient |
| NG4 | Exact multi-currency accounting | Conversions use daily reference rates for guidance; Wise's quote decides the amount paid |

---

## 3. User Groups & Personas

Access control uses **Django Groups and Permissions** plus a per-event **organiser** role. Groups are managed in Django admin and created automatically after migrations by `profile.groups.ensure_groups()`.

### 3.1 Group Definitions

| Group | Permissions | Typical capabilities |
|---|---|---|
| `Requester` | `submit_travel_requests` | Sign up, complete onboarding, create and edit own requests, accept approvals, report actual costs, upload documents, ask for an advance, submit for payment, propose events |
| `TSP Committee` | `submit_travel_requests`, `review_travel_requests`, `manage_events`, `view_reports` | Review and batch review requests, approve within or deliberately over budget, set advances, create, edit and delete events, assign organisers, read internal notes, view reports and the audit log |
| `Finance` | `view_reports`, `process_payments` | Set event budgets, process payments (Wise API, Wise batch, CSV, manual), settle advances and refunds, view reports and the audit log |

Users may belong to several groups. New sign-ups join **Requester** automatically. Nobody can pay a request of their own; another finance member has to.

### 3.2 Event Organisers

The committee can name **organisers** on an event (`Event.organizers`). An organiser:

- reviews, approves and rejects that event's requests, within the event's budget (only the committee can go over it)
- reads and writes internal notes on those requests
- is notified of new requests for the event
- keeps these rights until 90 days after the event ends (`Event.ORGANISER_DAYS_AFTER`)
- must use two-step sign-in while they hold the role

### 3.3 Personas

**Requester - "Douglas"**
- openSUSE contributor who speaks at conferences worldwide
- Signs up through an emailed confirmation link and a wizard (account, profile, Wise banking verification)
- Submits requests, accepts the approval, reports actual costs and uploads receipts after the event
- Gets email and, if enabled, push updates

**TSP Committee - "Admin"**
- Reviews requests per event cycle, often several at once
- Sets each event's share of costs and assigns organisers
- Watches budgets on the dashboard

**Event Organiser - "Local team lead"**
- Reviews requests for one event only, within its budget

**Finance - "Finance Team"**
- Sets budgets, pays approved requests and advances through Wise
- Sees the Wise balance against what upcoming events still need

---

## 4. System Architecture

### 4.1 High-Level Architecture

```
┌──────────────────────────────────────────────────────────────┐
│     Browser (Django templates, DaisyUI/Tailwind, htmx)       │
│  sign in -> dashboard -> request detail -> payments/profile  │
└────────────────────────────┬─────────────────────────────────┘
                             │ HTTPS
┌────────────────────────────▼─────────────────────────────────┐
│                  Django 6 (marvin project)                    │
│  ┌─────────────┐  ┌──────────────┐  ┌─────────────────────┐  │
│  │ tsp app     │  │ profile app  │  │ payments app        │  │
│  │ requests    │  │ UserProfile  │  │ gateways, adapters  │  │
│  │ events      │  │ groups, 2FA  │  │ Wise batches        │  │
│  │ reports     │  │ Wise services│  │ webhooks, Wise log  │  │
│  │ rates       │  │ email, push  │  └─────────────────────┘  │
│  └─────────────┘  │ audit log    │  ┌─────────────────────┐  │
│                   └──────────────┘  │ webpush (3rd party) │  │
│  django.contrib.admin - /admin/     └─────────────────────┘  │
├──────────────────────────────────────────────────────────────┤
│  marvin.audit - AuditModel + AuditUserMiddleware             │
├──────────────────────────────────────────────────────────────┤
│  SQLite (dev) or PostgreSQL (prod) · uploads under MEDIA_ROOT│
├──────────────────────────────────────────────────────────────┤
│  Wise API · SMTP · Browser Push (VAPID) · ECB/Frankfurter    │
└──────────────────────────────────────────────────────────────┘
```

### 4.2 Technology Stack

| Layer | Technology | Notes |
|---|---|---|
| Runtime | Python 3.13+ | `uv` or `pip` |
| Framework | Django 6 | Server-side rendered HTML |
| Database | SQLite in development, PostgreSQL in production | Credentials from `.env`; CI tests both |
| Auth | Django auth, Groups, django-otp | Username/password, two-step sign-in with TOTP and backup codes |
| UI | DaisyUI 5 + Tailwind 4 | Built ahead of time in `css_build/` to `static/css/styles.css`; themes `i-layer-light` / `i-layer-dark` |
| Live updates | htmx 2 | Dashboard and payment queue sections refresh themselves |
| Print | paper-css | Printable reimbursement form |
| Tables | django-tables2 | Search results |
| Email | Django SMTP mailer | STARTTLS on port 587 |
| Push | django-webpush | VAPID keys; opt-in per user |
| Payments | Pluggable gateway registry | Wise API, Wise CSV, manual transfer |
| Static files | WhiteNoise | Compressed manifest storage in production |
| Server | gunicorn | Production WSGI server |

### 4.3 Project Structure

```
marvin/                      # Project settings and shared utilities
├── settings.py              # Development and test defaults, read from the environment
├── prod_settings.py         # Production: DEBUG off, HTTPS, 2FA, live Wise, shared limits cache
├── audit.py                 # AuditModel + AuditUserMiddleware
├── branding.py              # Name, tagline, policy link, themes from the environment
├── validators.py            # Shared field validators
profile/                     # Identity, groups, onboarding, notifications, 2FA
├── models.py                # UserProfile (db_table: tsp_userprofile), notification kinds
├── groups.py                # Group definitions and helpers
├── two_factor.py            # Who must use two-step sign-in, and the middleware
├── audit_log.py             # AuditLogEntry, the append-only action log
├── services/
│   ├── wise_verification.py # Seven-step Wise recipient verification
│   ├── wise_recipient.py    # Check and recreate Wise recipients
│   ├── wise_banking_options.py # Bank and branch lists from Wise
│   ├── email_notifications.py
│   └── push_notifications.py
tsp/                         # Travel Support Program core
├── models.py                # Event, TravelRequest, Expense, ExchangeRate, StateHistory, Comment, Document
├── views.py                 # Sign-in, sign-up, dashboards, events, requests, reports, search, audit log
├── forms.py
├── middleware.py            # Onboarding redirect, stale bank details cleanup
├── payables.py              # TravelRequestPayableAdapter
├── services/
│   ├── state_machine.py     # Allowed transitions and history
│   └── exchange_rates.py    # Daily reference rates
├── management/commands/     # seed_data, fetch_exchange_rates, send_claim_reminders
├── fixtures/initial.json    # Demo users, groups, events, requests
payments/                    # Pluggable payments
├── models.py                # PaymentRecord, WiseLogEntry
├── registry.py              # Gateway and adapter loader
├── services.py              # PaymentService
├── webhooks.py              # Wise webhook signature check
├── gateways/                # wise_api.py, wise.py, manual.py, wise_logging.py
├── management/commands/     # verify_wise_recipient
css_build/                   # Tailwind/DaisyUI build (npm), pinned htmx and paper-css
templates/ static/           # Templates, compiled CSS, JS, images
.github/workflows/tests.yml  # CI
```

### 4.4 Deployment Model

**Development:**

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py seed_data      # demo data, password "demo"; --force to reload
uv run python manage.py runserver
```

**Production** uses `DJANGO_SETTINGS_MODULE=marvin.prod_settings`, which refuses to start without a real `DJANGO_SECRET_KEY` and a public `ALLOWED_HOSTS`. The README's Railway section lists the steps. In short:

- PostgreSQL in `prod_settings.py`, with only the credentials in `.env` (`DATABASE_URL` is not read)
- a persistent volume for `MEDIA_ROOT`
- start command: `python manage.py collectstatic --noinput && python manage.py migrate && python manage.py createcachetable && gunicorn marvin.wsgi`
- `createcachetable` makes the `marvin_limits` table that holds sign-in limits; without it nobody can sign in

---

## 5. Data Model

### 5.1 Entity-Relationship Diagram

```
auth.User ──1:1── profile.UserProfile
     │
     ├──M:N── auth.Group
     ├──M:N── Event (as organiser)
     ├──1:N── AuditLogEntry
     ├──1:N── webpush.PushInformation
     │
     └──1:N── TravelRequest ──1:N── Expense
                      │
                      ├──1:N── StateHistory
                      ├──1:N── Comment
                      ├──1:N── Document
                      └──1:N── PaymentRecord (GenericForeignKey)

Event ──1:N── TravelRequest
ExchangeRate (per day and currency, against EUR)
WiseLogEntry (every Wise API call and webhook)
```

All domain models in `profile`, `tsp` and `payments` inherit **`AuditModel`** (`marvin/audit.py`): `created_at`, `created_by`, `updated_at`, `changed_by`, filled in by `AuditUserMiddleware`.

### 5.2 Core Models

#### `profile.UserProfile`

| Field | Description |
|---|---|
| `user` | One-to-one with the Django user |
| `phone`, `country` | Contact details. The address columns (`address_line`, `city`, `state`, `postal_code`) are no longer written and are kept only for older accounts |
| `profile_completed_at` | When the profile step was completed |
| `muted_notifications` | Notification kinds the user switched off |
| `banking_method` | `iban`, `us_bank`, `india_ifsc`, `australia`, `brazil`, `indonesia`, `japan` |
| `banking_currency` | Payout currency |
| `wise_account_id`, `wise_recipient_verified_at` | Wise recipient and when it was verified |

**Not stored:** the postal address, IBAN, SWIFT, account numbers, routing or IFSC codes. These stay in the session only while Wise checks them, are dropped after an hour if the check is abandoned, and go straight to Wise.

Custom permissions: `submit_travel_requests`, `review_travel_requests`, `manage_events`, `view_reports`, `process_payments`.

#### `tsp.Event`

Name, location, dates, `url`, `description`, `accepting_requests`, `requests_open_until`, `reimbursements_open_until`, `visa_letters_offered`, `extra_expense_categories`, `budget` and `budget_currency` (set by finance), `suggested_budget` and `proposed_by` (for proposals), `approval_percent` (the share of each requested amount reviews start from), and `organizers`.

#### `tsp.TravelRequest`

Owner, event, talk title and URL, description, `state`, `approved_at`, `approval_percent`, `accepted_at` / `accepted_by`, advance fields (`advance_amount`, `advance_currency`, `advance_requested_at`), `claim_reminder_days`, and `refund_settled_at`. Its number is shown as a reference such as `TSP643`.

#### `tsp.Expense`

Category, description, and three amounts each with a currency: `estimated`, `approved` and `actual`. The approval locks the exchange rate (`approval_rate`, `approval_rate_date`). A line is paid at the lesser of approved and actual.

#### `tsp.Comment`, `tsp.Document`, `tsp.StateHistory`, `tsp.ExchangeRate`

- `Comment` has `internal` for notes the requester never sees.
- `Document` stores `kind`, the file, original name, MIME type, size and uploader.
- `StateHistory` records each transition with notes.
- `ExchangeRate` stores one rate per day and currency against the euro.

#### `payments.PaymentRecord`

Linked to any payable through a GenericForeignKey. Fields: `gateway_id` (`wise-api`, `wise-csv`, `manual`), `amount`, `currency`, `recipient_name`, `recipient_email`, `reference`, `external_id` (Wise transfer ID), `status`, `purpose` (`advance` or a regular payment), `metadata`, `processed_by`. Payment history is protected from the admin and event deletes.

#### `payments.WiseLogEntry` and `profile.AuditLogEntry`

- `WiseLogEntry` keeps every Wise request and webhook delivery with timing, status and signature result.
- `AuditLogEntry` is append-only: action, target, detail, user and time.

### 5.3 Seed Data

`python manage.py seed_data` loads `tsp/fixtures/initial.json` and sets every password to `demo` (`--force` flushes and reloads).

| Username | Group | Notes |
|---|---|---|
| `admin` | TSP Committee | Staff and superuser |
| `finance_team` | Finance | |
| `douglasd`, `jane_doe`, `patrickf` | Requester | Demo requesters with requests in several states |
| `superuser` | Requester | Staff superuser; can skip Wise banking |

**Events:** openSUSE Conference 2026 and 2027, FOSDEM 2026, LinuxConf.au 2026.

---

## 6. Request Lifecycle - State Machine

### 6.1 States

| State | Owner | Description |
|---|---|---|
| `draft` | Requester | Being edited; not visible to reviewers |
| `submitted` | Reviewers | Awaiting the committee or an organiser |
| `approved` | Requester | Approved per line; awaiting acceptance |
| `rejected` | Terminal | Rejected with notes |
| `accepted` | Requester | Accepted; actual costs and documents to add, advance may be asked for |
| `submitted_for_payment` | Finance | Ready to pay |
| `paid` | Terminal | Paid |
| `cancelled` | Terminal | Cancelled |

### 6.2 Transitions

Defined in `tsp/services/state_machine.py`. Each transition writes `StateHistory` and the audit log and sends notifications.

| From | To | Actor | Trigger |
|---|---|---|---|
| *(create)* | `draft` | Requester or committee | `/requests/new/` |
| `draft` | `submitted` | Owner | `/requests/<id>/submit/` |
| `submitted` | `approved` / `rejected` | Committee or event organiser | `/requests/<id>/approve/`, `/reject/`, or `/requests/batch/` |
| `approved` / `accepted` | `submitted` | Owner | Changing the amounts after approval sends it back for review |
| `approved` | `accepted` | Owner | `/requests/<id>/accept/confirm/` |
| `accepted` | `submitted_for_payment` | Owner | `/requests/<id>/submit-for-payment/` |
| `submitted_for_payment` | `accepted` | Owner | `/requests/<id>/take-back/`, only before finance starts paying |
| `submitted_for_payment` | `paid` | Finance | `/payments/process/`, a Wise batch, or `/requests/<id>/settle/` |
| `paid` | `submitted_for_payment` | System | A Wise payment returned (`reopen_for_payment`) |
| any non-terminal | `cancelled` | Owner or committee; finance once it waits for payment | `/requests/<id>/cancel/`, refused while a payment is in progress |

### 6.3 Editability and Concurrency

- The requester edits a request while it is `draft` or `submitted`, and may change amounts while `approved` or `accepted` until money has moved.
- Approvals lock the event row, so two approvals at once cannot both pass the budget.
- Edits, take-backs and the payment queue lock the request row, so a payment starting at the same moment is always seen.

---

## 7. Feature Requirements

### 7.1 Authentication & Sign-Up

- One sign-in page at `/login/` for the app and the admin (`/admin/login/` redirects there).
- Sign-in attempts are limited per account and per address. In production the counts are shared by all workers and survive deploys. The account owner can still sign in from their own address during an attack.
- Two-step sign-in (TOTP app and backup codes) is required for the committee, finance and active organisers, and optional for others. Production enforces it (`TWO_FACTOR_ENFORCED`).
- Password reset by email; changing the email address needs a link sent to the new address.

**Sign-up (`/signup/`):**
1. Account (username, email, password), activated by an emailed link
2. Profile (name, phone, country)
3. Wise banking: method, details and postal address, held in the session only (Wise needs the address to create the payee)
4. Seven-step Wise verification with a progress view (see §10.2)
5. Completion page

Staff and superusers may skip banking (`/signup/banking/skip/`). The onboarding middleware sends incomplete requesters to the right step.

### 7.2 Dashboards

One page at `/` with a section for each group the user belongs to:

| Section | Content |
|---|---|
| Requester | Own requests with the next action, claim deadlines, any advance owed back |
| Committee | Review queue with batch approve/reject, events from the last 90 days with requests and money against budget |
| Organiser | Requests for the events they organise |
| Finance | Payment queue, the Wise balance and where it falls short of what upcoming events still need, recent payments |

Lists are paginated. Sections refresh through htmx while nobody is typing in them, and status changes are announced to screen readers.

### 7.3 Request Creation & Editing

- Event dropdown limited to events open for requests, talk fields, expense lines, description
- Expense categories: flight, train or bus, lodging, other, plus any extra categories set on the event
- Amounts can be in any supported currency; the form previews the total in the requester's payout currency
- **Check account** confirms the Wise recipient still exists and recreates it if needed (changing it requires the current password)

### 7.4 Review & Approval

- The committee, or an event's organiser, sets approved amounts per line, starting from the event's share (`approval_percent`)
- Approvals that would exceed the event's budget are refused; the committee may override deliberately, and the overage is written into the history
- The committee may allow an advance at approval
- Reject requires notes; batch review approves or rejects several requests at once

### 7.5 Acceptance, Actual Costs & Advances

- The requester accepts on a page with required confirmations
- After the event they report what each approved line actually cost (`/requests/<id>/actuals/`)
- If an advance was allowed they can ask finance to pay it before the event; once paid, the final payment is reduced by it, and any amount owed back is shown and settled by finance (`/requests/<id>/settle/`)
- Claims must be submitted before the event's reimbursement deadline: `reimbursements_open_until`, or `REIMBURSEMENT_WINDOW_DAYS` (default 30) after the event ends

### 7.6 Comments & Documents

- Comment thread on each request; reviewers and finance can add **internal notes** the requester never sees
- Document upload with kind, and deletion of a mistaken upload
- Receipt, invoice and signed form are all required before submitting for payment

### 7.7 Events

- List with date filters; past events dimmed
- The committee creates, edits and deletes events (delete only when nobody has applied), sets the share of costs and assigns organisers
- Finance sets the budget; a person in both groups can do both
- Anyone else can **propose** an event with a suggested budget; it stays closed until the committee opens it

### 7.8 Profile, Banking & Notifications

- Personal details, email change with confirmation, password change by email link
- Two-step sign-in setup, backup codes, and turning it off when not required
- Wise recipient status (account ID, verification date), never raw bank numbers
- Notification settings: browser push opt-in and muting each kind of alert

### 7.9 Search

`/search/` finds requests and events; staff may also search users.

### 7.10 UI, Theming & Accessibility

- DaisyUI layout with a left navigation, skip link, account menu and light/dark toggle
- Branding from the environment (`BRAND_APP_NAME`, `BRAND_ORG_NAME`, `BRAND_TAGLINE`, `BRAND_STRAPLINE`, `BRAND_POLICY_URL`, `BRAND_THEME_LIGHT`, `BRAND_THEME_DARK`)
- Destructive actions confirm in a modal dialog; forms show errors linked to their fields
- Login page uses a full-screen background photo (`static/img/waiting.webp`)

### 7.11 Printable Reimbursement Form

`/requests/<id>/form/` renders the agreement with approved expenses for printing and signing.

---

## 8. Route Reference

### 8.1 Authentication & Sign-Up

| Path | Description |
|---|---|
| `/login/`, `/logout/` | Sign in and out |
| `/password/reset/...` | Password reset by email |
| `/signup/` | Account step |
| `/signup/confirm/<uidb64>/<token>/`, `/signup/confirm/resend/` | Activation link |
| `/signup/profile/` | Profile step |
| `/signup/banking/`, `/signup/banking/options/` | Banking step and Wise bank lists |
| `/signup/banking/progress/`, `/signup/banking/step/` | Verification progress and one step (JSON) |
| `/signup/banking/skip/` | Skip banking (staff) |
| `/signup/complete/` | Finished |

### 8.2 Core Routes

| Path | Who | Description |
|---|---|---|
| `/` | Signed in | Dashboard |
| `/events/`, `/events/new/` | Signed in | List; create (committee) or propose (others) |
| `/events/<id>/edit/`, `/events/<id>/delete/` | Committee, finance for budget | Edit, delete |
| `/search/` | Signed in | Search |
| `/requests/new/` | Requester, committee | Create |
| `/requests/check-account/` | Requester, committee | Wise recipient check |
| `/requests/batch/` | Committee, organisers | Batch approve/reject |
| `/requests/<id>/` | Owner, reviewers, finance | Detail |
| `/requests/<id>/edit/`, `/submit/` | Owner | Edit, submit |
| `/requests/<id>/approve/`, `/reject/` | Committee, organiser | Review |
| `/requests/<id>/cancel/` | Owner, committee, finance | Cancel |
| `/requests/<id>/accept/`, `/accept/confirm/` | Owner | Accept |
| `/requests/<id>/actuals/` | Owner | Actual costs |
| `/requests/<id>/advance/` | Owner | Ask for the advance |
| `/requests/<id>/submit-for-payment/`, `/take-back/` | Owner | Into and out of the payment queue |
| `/requests/<id>/settle/` | Finance | Close a request the advance covered, or record a refund |
| `/requests/<id>/comments/` | Owner, reviewers, finance | Comment or internal note |
| `/requests/<id>/documents/`, `/documents/<doc_id>/delete/` | Owner, reviewers, finance | Upload, delete |
| `/requests/<id>/form/` | Owner, reviewers, finance | Printable form |
| `/documents/<id>/download/` | Owner, reviewers, finance | Download |
| `/reports/` | Committee, finance | Reports (`?format=csv`) |
| `/audit-log/` | Committee, finance | Audit log |

### 8.3 Profile Routes (`/profile/`)

| Path | Description |
|---|---|
| `/profile/` | Profile and banking |
| `/profile/notifications/` | Notification settings |
| `/profile/email/confirm/<token>/` | Confirm a new email address |
| `/profile/password/` | Email a password change link |
| `/profile/two-step/setup/`, `/verify/`, `/backup-codes/`, `/off/` | Two-step sign-in |

### 8.4 Payment Routes (`/payments/`)

| Path | Description |
|---|---|
| `/payments/` | Payment queue |
| `/payments/process/` | Pay one request |
| `/payments/send-to-wise/` | Create Wise batch groups for everything waiting |
| `/payments/batch-export/` | Wise CSV download |
| `/payments/<id>/status/` | Refresh one payment's status |
| `/payments/webhooks/wise/` | Wise webhook (signature checked) |
| `/payments/gateways/<id>/currencies/` | Supported currencies |

### 8.5 Other

| Path | Description |
|---|---|
| `/webpush/save_information` | Save a browser subscription (signed in) |
| `/webpush/service-worker.js` | Service worker |
| `/admin/` | Django admin |

---

## 9. Authentication & Authorization

### 9.1 Mechanisms

1. **Django Groups** (`profile/groups.py`) and custom permissions on `UserProfile`
2. **Event organisers** (`Event.organizers`, time-limited)
3. **View decorators**: `login_required`, `group_required(...)`, `staff_or_owner`
4. **Payment permission callback**: `PAYMENTS_PERMISSION_CHECK = 'profile.permissions.finance_can_process_payments'`
5. **Two-step middleware** holds users who need it at the code prompt
6. **Rate limits** on sign-in, sign-up and bank detail changes

### 9.2 Authorization Matrix

| Action | Requester | Organiser | Committee | Finance |
|---|---|---|---|---|
| Create request | ✓ | | ✓ | |
| Edit own request | ✓ | | | |
| Approve/reject | | ✓ (own events, within budget) | ✓ | |
| Go over budget | | | ✓ | |
| Internal notes | | ✓ | ✓ | ✓ |
| Cancel | ✓ (own) | | ✓ | ✓ (in payment queue) |
| Accept, actuals, advance, submit for payment | ✓ (own) | | | |
| Create/edit events | propose | | ✓ | budget only |
| Process payments, settle | | | | ✓ (never their own) |
| Reports, audit log | | | ✓ | ✓ |
| Two-step sign-in | optional | required | required | required |

---

## 10. Payment Integration - Wise

### 10.1 Architecture

Payments use a **plugin registry** (`payments/registry.py`):

- **Gateways** - `WiseApiGateway`, `WiseCsvGateway`, `ManualTransferGateway`
- **Adapters** - `TravelRequestPayableAdapter` exposes amount, recipient, reference and purpose
- **PaymentRecord** - one row per payment attempt

```python
PAYMENTS_GATEWAYS = [
    'payments.gateways.wise.WiseApiGateway',
    'payments.gateways.wise.WiseCsvGateway',
    'payments.gateways.manual.ManualTransferGateway',
]
PAYMENTS_ADAPTERS = ['tsp.payables.TravelRequestPayableAdapter']
PAYMENTS_PERMISSION_CHECK = 'profile.permissions.finance_can_process_payments'
```

### 10.2 Recipient Verification (Onboarding)

`profile/services/wise_verification.py` runs seven steps:

1. Create Wise payee account
2. Confirm bank details with Wise
3. Request exchange quote for test transfer
4. Create test transfer
5. Fund test transfer (sandbox only; skipped on a live account so no money moves)
6. Cancel / reverse test transfer
7. Save verification to the profile

Changing an existing payout account requires the current password, is rate limited, and is recorded in the audit log with an email to the account owner.

### 10.3 Paying

| Gateway | ID | Description |
|---|---|---|
| Wise API | `wise-api` | Creates a quote and transfer per request; finance approves funding in Wise |
| Wise batch | `wise-api` | `/payments/send-to-wise/` groups everything waiting into one Wise batch per currency |
| Wise CSV | `wise-csv` | `Send-by-email.csv` export |
| Manual | `manual` | Records a payment made outside Marvin |

- Each payment goes out in the requester's payout currency, converted from the approved amounts.
- Wise is called outside the database lock, so a slow response does not hold up the queue.
- Transfer status arrives through the signed webhook, and is also refreshed for a few transfers on each visit to the queue.
- A returned payment puts the request back in the queue.

### 10.4 Exchange Rates

`tsp/services/exchange_rates.py` stores daily reference rates against the euro from Frankfurter (ECB), or today's rate from ExchangeRate-API when `EXCHANGE_RATE_API_KEY` is set. `python manage.py fetch_exchange_rates` fills them. Rates are for guidance; Wise's quote decides what is paid.

### 10.5 Configuration

| Setting | Default | Description |
|---|---|---|
| `USE_WISE` | `true` | `false` runs without Wise: no bank step at sign-up, no Wise gateways, pages or webhook; payments are recorded manually |
| `WISE_API_TOKEN` | Published sandbox token | Must be set in any deployment |
| `WISE_API_PROFILE_ID` | *(auto)* | Wise business profile |
| `WISE_API_SANDBOX` | `true` in development, `false` in production | Sandbox or live API |
| `WISE_SOURCE_CURRENCY` | `EUR` | Currency payments are funded from |
| `WISE_WEBHOOK_PUBLIC_KEY` | *(empty)* | Webhooks are refused without it |
| `WISE_BALANCE_FETCH` | off in development, on in production | Show the Wise balance to finance |
| `PAYOUT_CURRENCIES` | `EUR,USD,GBP` | Currencies the committee approves in |

---

## 11. Notifications

### 11.1 Channels

- **Email** for state changes, activation, password and email changes, and claim deadline reminders
- **Browser push** (django-webpush) for users who enable it on their profile and subscribe a browser

Each kind below can be muted per user. Anything not listed, such as a payment coming back, is always sent.

### 11.2 Notification Kinds

| Kind | Recipients |
|---|---|
| A request is submitted for review | Committee and the event's organisers |
| My request is approved / rejected | Requester |
| A request is accepted | Committee |
| A request is ready for payment | Finance |
| My request is paid | Requester |
| A request is cancelled | Requester or committee |
| A new comment | The other party |
| A new internal note | Committee, finance and organisers, never the requester |

### 11.3 Claim Reminders

`python manage.py send_claim_reminders` emails requesters 14, 7 and 1 days before their reimbursement deadline (`--dry-run`, `--days`). It is meant to run daily from a scheduler.

---

## 12. Document Management

| Kind | Required for payment |
|---|---|
| `receipt` | Yes |
| `invoice` | Yes |
| `signed_form` | Yes |

- PDF, PNG or JPEG only, checked by extension and file signature; 10 MB at most
- Stored under `MEDIA_ROOT/<request_id>/`; production needs a persistent volume
- Downloads limited to the owner, reviewers and finance

---

## 13. Reporting, Search & Audit

- **Reports** (`/reports/`, committee and finance): filter by event dates, status and text; summary totals; CSV export
- **Search** (`/search/`): requests and events for everyone, users for staff
- **Audit log** (`/audit-log/`, committee and finance): every recorded action, newest first, never edited
- **Wise log**: every Wise call and webhook in `WiseLogEntry`, plus `logs/wise.log` and the console

---

## 14. Known Gaps & Limitations

### 14.1 Security

| Gap | Notes |
|---|---|
| Published defaults | `settings.py` keeps a sandbox Wise token and dev VAPID keys as fallbacks on purpose, so an instance boots without Wise or push set up. A real deployment sets its own in `.env` or the host's variables |
| Demo password | Seeded users share `demo`; never load the fixture with that password in production |

### 14.2 Features

| Gap | Notes |
|---|---|
| No SSO | Username/password with two-step sign-in |
| Wise funding | Personal-token accounts cannot fund through the API; finance approves payments in Wise |
| Push needs HTTPS | Browsers require a secure context |
| Claim reminders need a scheduler | The command does not schedule itself |

### 14.3 Operations

| Gap | Notes |
|---|---|
| Disk storage | Uploads need a persistent volume; no S3 backend |
| `DATABASE_URL` | Not read; set the separate `DATABASE_*` variables |

---

## 15. Roadmap

### Done

| Task |
|---|
| Username/password sign-in with two-step verification |
| PostgreSQL support, tested in CI |
| Environment-based secrets and production settings |
| Upload validation (type, signature, size) |
| Pagination |
| Automated test suite (450+ tests) and CI (SQLite, PostgreSQL, deploy requirements, built CSS) |
| Email notifications and muting |
| Event edit/delete, proposals, budgets and organisers |
| Batch approve/reject |
| Internal notes |
| Wise batches, webhooks and status tracking |
| Advances, actual costs and refunds |
| Configurable branding |
| Append-only audit log |
| `USE_WISE` switch to run without Wise |

### Next

| Task | Priority |
|---|---|
| First production deployment and end-to-end test against Wise | High |
| Schedule `send_claim_reminders` and `fetch_exchange_rates` | Medium |
| SSO/OAuth (Keycloak, GitHub) | Medium |
| S3-compatible document storage | Medium |
| REST/JSON API | Low |
| i18n | Low |

---

## Appendix A: Environment Variables

`.env.example` documents each variable. The main ones:

| Variable | Default | Description |
|---|---|---|
| `DJANGO_SETTINGS_MODULE` | `marvin.settings` | `marvin.prod_settings` in production |
| `DJANGO_SECRET_KEY` | Dev key | Required in production |
| `DEBUG` | `true` | Always off in production |
| `ALLOWED_HOSTS` | Local hosts | Required in production |
| `CSRF_TRUSTED_ORIGINS` | *(empty)* | Only for plain http or a non-standard port |
| `DATABASE_USER`, `DATABASE_PASSWORD` | none | PostgreSQL credentials in production |
| `DATABASE_NAME`, `DATABASE_HOST`, `DATABASE_PORT` | `marvin`, `localhost`, `5432` | Only when they differ |
| `MEDIA_ROOT` | `uploads/` | Document storage |
| `EMAIL_HOST`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `DEFAULT_FROM_EMAIL` | localhost | SMTP |
| `SITE_BASE_URL` | `WEBPUSH_APP_BASE_URL` | Links in emails |
| `TWO_FACTOR_ENFORCED` | off in development, on in production | Require two-step sign-in |
| `REIMBURSEMENT_WINDOW_DAYS` | `30` | Days after an event to claim |
| `WISE_*`, `PAYOUT_CURRENCIES` | See §10.5 | Wise |
| `EXCHANGE_RATE_API_KEY`, `EXCHANGE_RATES_FETCH` | *(empty)*, off in development | Exchange rates |
| `VAPID_PUBLIC_KEY`, `VAPID_PRIVATE_KEY`, `VAPID_ADMIN_EMAIL`, `WEBPUSH_APP_BASE_URL` | Dev keys | Push |
| `BRAND_*` | Geeko Foundation values | Branding |

## Appendix B: Management Commands

| Command | Description |
|---|---|
| `migrate` | Apply migrations (groups are created afterwards) |
| `createcachetable` | Create the sign-in limits table used in production |
| `collectstatic` | Collect static files for production |
| `seed_data [--force]` | Load the demo fixture and set passwords to `demo` |
| `fetch_exchange_rates` | Store daily reference rates |
| `send_claim_reminders [--dry-run] [--days ...]` | Email approaching claim deadlines |
| `verify_wise_recipient` | Check a user's Wise recipient |
| `createsuperuser`, `check` | Standard Django commands |

## Appendix C: Expense Categories

| Category | Label |
|---|---|
| `flight` | Flight |
| `train` | Train or Bus |
| `lodging` | Lodging |
| `other` | Other |

Events can add their own categories through `extra_expense_categories`.
