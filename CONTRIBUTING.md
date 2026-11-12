# Contributing to Marvin

Marvin is a travel support programme for the Geeko Foundation, written so that
other organisations can run their own instance. Contributions are welcome,
whether you run Marvin yourself, want to fix something that bit you, or are
coming from the openSUSE Travel Support Program it is based on.

## Getting set up

The README covers installation in full. The short version:

```bash
uv sync
uv run python manage.py migrate
uv run python manage.py seed_data
uv run python manage.py runserver
```

`seed_data` loads the demo fixture and sets every user's password to `demo`, so
you can sign in as `admin` and look around. It gives you a requester, a
committee member and a finance user, which is the quickest way to see how a
request moves through the system.

No configuration is needed to run locally. SQLite is the default and `.env` is
optional; `.env.example` documents every setting a deployment can override.

## Before you open a pull request

Run what CI runs:

```bash
uv run python manage.py check
uv run python manage.py makemigrations tsp profile payments --check --dry-run
uv run python manage.py test
```

The migration check is scoped to the project's own apps on purpose. An unscoped
`makemigrations --check` also inspects third-party apps, where `django-webpush`
reports a field alteration that cannot be fixed from this repository.

CI runs the whole suite against SQLite and PostgreSQL on every pull request.

## What a good change looks like

**One change per pull request.** A branch that fixes a bug and tidies something
unrelated is harder to review and harder to revert if it turns out to be wrong.

**Tests for behaviour that matters.** Anything touching the request state
machine, money, permissions or the Wise integration needs a test. The existing
suites are a reasonable guide: `tsp/tests.py` for the lifecycle and
permissions, `payments/` for gateways and payment records, and the
`tsp/test_*.py` modules for individual features.

**Migrations that do not lose data.** If a change removes a field or a choice,
say what happens to rows already using it. Prefer moving the information
somewhere it survives over deleting it, and make the migration reversible
where that is possible.

**Comments that explain why.** The code already says what it does. A comment
earns its place by recording a decision, a constraint or something surprising,
such as why a value is read back from an API rather than trusted from a
payload.

## Commit messages and pull requests

Write a subject line that says what changed, in the imperative, under about 70
characters. Use the body to explain the problem the change solves and anything
a reviewer would otherwise have to work out for themselves.

Pull request descriptions should state the problem, what the change does, and
how it was verified. Reference related pull requests and issues by number.

## Reporting a bug

Open an issue describing what you did, what you expected and what happened
instead. If it involves a payment or a Wise recipient, include the currency and
the country, since most of the problems found so far have been specific to one
corridor. Do not include bank account numbers, Wise recipient details or any
other personal data.

## Security

If you find a vulnerability, especially one touching payments, banking details
or access control, report it privately to the maintainers rather than opening a
public issue.

## Things worth knowing

**Number migrations after whatever is on master when you merge.** Two
branches that each add a migration with the same number merge without any Git
conflict, because they are separate files, but Django then refuses to migrate
with two leaf nodes in the graph. Before merging, rebase or merge master in and
run `makemigrations --check`. If you renumber a migration that may already have
been applied somewhere, declare its old name in `replaces` so that database
does not try to apply it a second time.

**The state machine is the choke point.** Every workflow transition goes through
`transition_request()` in `tsp/services/state_machine.py`. Add a transition
there rather than setting `state` directly, or the history, notifications and
permission checks are skipped.

**Branding is configuration, not code.** Names, taglines and themes come from
settings so a fork can rebrand without editing templates. New user-facing text
should not hardcode "Marvin" or "Geeko Foundation".

**Banking details are not stored here.** Marvin keeps a Wise recipient ID and
metadata. Account numbers live with Wise. Keep it that way.

**The Wise sandbox is the real test.** The API accepts and rejects things its
documentation does not fully describe. If you change how a recipient or a
transfer is built, exercise it against the sandbox rather than reasoning about
it.
