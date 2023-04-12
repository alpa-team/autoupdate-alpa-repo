## GitHub Action for automatic updates of your packages in Alpa repository

Asynchronous action for updating [Alpa](https://github.com/alpa-team)
repository through Packit <-> Copr
integration with email notification. New updates are checked via
[Anytia](https://release-monitoring.org/).

### Who is this action for

For anybody who want their own [Alpa](https://github.com/alpa-team) repository.

### Use this action wisely

This action is mainly designed for cron jobs. In case of other triggers, such
as push or similar, with a large Alpa repository (hundreds of packages) you
can - and probably will - soon hit the monthly limit for GitHub Actions, which
is 3000 minutes for the free version. Since updating a package via Copr is
a time-consuming thing, even with the asynchronous implementation of this
action, this limit will be exceeded very easily with triggers like push, etc.

### Workflow yaml example

```yaml
name: Autoupdate Alpa repository

on:
  schedule:
    # every month on 10th day of the month, at 04:04 AM
    - cron: "4 4 10 * *"

jobs:
  update:
    runs-on: ubuntu-latest

    steps:
      - name: Checkout repo
        uses: actions/checkout@v3

      - name: Autoupdate Alpa repository
        uses: alpa-team/autoupdate-alpa-repository@<tag_name>
        with:
          email-name: example@water.xx
          smtp-address: smtp.water.xx
          email-password: ${{ secrets.ALPA_MAIL_BOT_PASSWORD }}
          debug: true
```

### Options

#### email-name

_required_

This is email address of your bot account. You have to create email account
to be notified of failed updates and I really recommend to create separate
email for this because of security reasons.

#### smtp-address

_required_

smtp address of the mail provider. For gmail it is `smtp.gmail.com

#### email-password

_required_

Warning! Don't you dare to store here your password directly even if it is
just dummy email! Use secrets to store the email password there.

#### debug

_not required_

Set to `true` if you want to see debug logs. Otherwise set to `false`.
