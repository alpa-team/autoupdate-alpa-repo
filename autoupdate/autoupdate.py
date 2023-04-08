import asyncio
import logging
import subprocess
import sys
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from enum import Enum
from os import getenv, getcwd, environ
from pathlib import Path
from smtplib import SMTP_SSL, SMTP_SSL_PORT
from ssl import create_default_context
from time import sleep
from typing import Optional

from aiohttp import ClientSession
from alpa.repository import LocalRepo
from alpa_conf import MetadataConfig
from alpa_conf.exceptions import AlpaConfException
from packaging.version import parse
from specfile import Specfile


if getenv("INPUT_DEBUG") == "true" or getenv("RUNNER_DEBUG") == "1":
    logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
else:
    logging.basicConfig(level=logging.ERROR, stream=sys.stdout)

logger = logging.getLogger(__name__)


EXIT_SUCCESS = 0
MAX_RETRY = 700

UPDATE_BRANCH_PREFIX = "__alpa_autoupdate"

CHECK_RUN_RUNNING_CONCLUSION = ["queued", "in_progress"]

GH_HEADERS = {
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

HTML_FOOTNOTE = """
<html>
  <body>
    <h4>
      This is automatically generated email via alpa-autoupdate tool.
      Don't reply to this email.
    </h4>
    If you want to know more about alpa project, please visit
    <a href="https://github.com/alpa-team">our GitHub organization</a>.
  </body>
</html>
"""
MAIL_BODY = (
    "Hello! We want to notify you, that your scheduled update "
    "of package {pkg_name} failed"
)


class RequestEnum(str, Enum):
    GET = "GET"
    POST = "POST"


class MailClient:
    def __init__(self) -> None:
        self.sender = environ["INPUT_EMAIL_NAME"]
        self.smtp_address = environ["INPUT_SMTP_ADDRESS"]

    def _prepare_mail(self, receiver: str, topic: str, body: str) -> MIMEMultipart:
        mail = MIMEMultipart()
        mail["From"] = self.sender
        mail["To"] = receiver
        mail["Subject"] = topic
        html_message = MIMEText(HTML_FOOTNOTE, "html", "utf-8")
        mail.attach(MIMEText(body, "plain", "utf-8"))
        mail.attach(html_message)
        return mail

    # TODO: could be asynchronous but I won't install flask/django because of
    #  sending mail
    def send_email(self, receiver: str, topic: str, body: str) -> None:
        c = create_default_context()
        with SMTP_SSL(self.smtp_address, SMTP_SSL_PORT, context=c) as srv:
            srv.login(self.sender, environ["INPUT_EMAIL_PASSWORD"])
            mail = self._prepare_mail(receiver, topic, body)
            srv.sendmail(self.sender, receiver, mail.as_string())


class Autoupdator69:
    def __init__(self) -> None:
        self.cwd = Path(getcwd())
        self.local_repo = LocalRepo(self.cwd)
        self.pkg_commit_sha: dict[str, str] = {}
        self.mail_client = MailClient()

    @staticmethod
    async def _async_requester(
        api_url: str, params: dict, method: RequestEnum
    ) -> tuple[dict, int]:
        async with ClientSession() as session:
            session_method = session.get if RequestEnum.GET == method else session.post
            logger.debug(f"Requesting {method} {api_url} with params {params}")
            async with session_method(api_url, params=params) as response:
                logger.info(f"Response status: {response.status}")
                return await response.json(), response.status

    async def _get_package_last_version(
        self, pkg_name: str, backend: str
    ) -> Optional[str]:
        url = "https://release-monitoring.org/api/projects/"
        query = {"pattern": pkg_name}
        resp, status = await self._async_requester(url, query, RequestEnum.GET)
        if status != 200:
            logger.error("Request status was not equal to 200")
            return None

        projects = resp["projects"]
        for project in projects:
            if (
                project["name"].casefold() == pkg_name.casefold()
                and project["backend"].casefold() == backend.casefold()
            ):
                pkg_version = project["version"]
                logger.info(f"Package {pkg_name} found with version {pkg_version}")
                return pkg_version

        logger.warning(f"No package {pkg_name} found with backend {backend}")
        return None

    # this will bump version in spec file which will do the job with updating
    # because of script that downloads source when Copr builds SRPM
    def _update_version_of_package(
        self, specfile: Specfile, last_version_from_anytia: str, pkg_name: str
    ) -> str:
        # TODO: use some error-proof wrapper for self.git_cmd instead of
        #  using subprocess
        subprocess.run(
            ["git", "switch", "-c", f"{UPDATE_BRANCH_PREFIX}_{pkg_name}"], cwd=self.cwd
        )
        specfile.update_tag("Version", last_version_from_anytia)
        specfile.save()

        self.local_repo.git_cmd.add(f"{pkg_name}.spec")
        index = self.local_repo.local_repo.index
        index.commit(
            f"[alpa]: autoupdate of package {pkg_name} to "
            f"version {last_version_from_anytia}",
        )
        return self.local_repo.git_cmd.log("--pretty=format:'%H'", "-n", "1").strip("'")

    async def _push_changes(self, branch_to_push: str) -> bool:
        async_subprocess = await asyncio.create_subprocess_exec(
            "git",
            "push",
            self.local_repo.remote_name,
            branch_to_push,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )
        stdout, stderr = await async_subprocess.communicate()
        if async_subprocess.returncode == 0:
            logger.info(stdout.decode())
            return True

        logger.error(stderr.decode())
        return False

    async def _push_update(self, pkg_name: str) -> bool:
        self._ensure_switching_to_branch(pkg_name)
        logger.info(f"Merging update branch of package {pkg_name} with package branch")
        update_branch = f"{UPDATE_BRANCH_PREFIX}_{pkg_name}"
        subprocess.run(["git", "merge", update_branch], cwd=self.cwd)

        push_result = await self._push_changes(pkg_name)
        async_subprocess = await asyncio.create_subprocess_exec(
            "git",
            "push",
            self.local_repo.remote_name,
            "--delete",
            update_branch,
            cwd=self.cwd,
        )
        stdout, stderr = await async_subprocess.communicate()
        if async_subprocess.returncode == 0:
            logger.info(stdout.decode())
        else:
            logger.error(stderr.decode())

        if not push_result:
            # probably some merge conflict
            # TODO: pull and rebase should do the job
            return False

        return True

    def _cancel_update(self, pkg_name: str) -> None:
        pass

    async def _wait_for_check_run_and_push_update(
        self, pkg_name: str
    ) -> Optional[bool]:
        url = (
            f"https://api.github.com/repos/{self.local_repo.namespace}/"
            f"{self.local_repo.repo_name}/commits/{self.pkg_commit_sha.get(pkg_name)}/"
            f"check-runs"
        )
        resp, status = await self._async_requester(url, GH_HEADERS, RequestEnum.GET)
        if status != 200:
            logger.error("Response status was not 200")
            return False

        for check_run in resp["check_runs"]:
            if check_run["conclusion"] == "failure":
                return False

            if check_run["status"] in CHECK_RUN_RUNNING_CONCLUSION:
                # still no respose, waiting (don't log this to avoid spam pls)
                return None

        logger.info(f"Package {pkg_name} was successfully updated for all chroots")
        return await self._push_update(pkg_name)

    def _get_metadata_config(self, pkg_name: str) -> Optional[MetadataConfig]:
        self._ensure_switching_to_branch(pkg_name)
        try:
            return MetadataConfig.get_config(self.cwd)
        except (FileNotFoundError, AlpaConfException) as exc:
            logger.error(f"Exception during handling metadata.yaml occurred: {exc}")
            return None

    def _notify_maintainers(self, maintainer_emails: list[str], pkg_name: str) -> None:
        try:
            for maintainer_mail in maintainer_emails:
                self.mail_client.send_email(
                    maintainer_mail,
                    f"[Alpa-autoupdate] Your update of package {pkg_name} failed",
                    MAIL_BODY.format(pkg_name=pkg_name),
                )
        except Exception as exc:
            logger.error(f"Sending mail failed: {exc}")

    async def wait_for_check_run_to_end(self, pkg_name: str) -> bool:
        # we have only 3000 free minutes on GH actions so fuck long builds
        for _ in range(MAX_RETRY):
            result = await self._wait_for_check_run_and_push_update(pkg_name)
            if result is None:
                # builds usually takes at least 2 minutes
                await asyncio.sleep(120)
                continue

            if result:
                return True

            self._cancel_update(pkg_name)
            metadata = self._get_metadata_config(pkg_name)
            if metadata is not None:
                self._notify_maintainers(
                    [user.email for user in metadata.maintainers], pkg_name
                )

            return False

        logger.error(
            f"Update of {pkg_name} package timeouted after {MAX_RETRY} retries"
        )
        return False

    # since this code is asynchronous, a lot of switching to branches happens.
    # This is why this method appears on multiple places so often
    def _ensure_switching_to_branch(self, branch: str) -> None:
        subprocess.run(
            ["git", "switch", branch],
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
        )

    async def update_package(self, pkg_name: str) -> bool:
        pkg_metadata = self._get_metadata_config(pkg_name)
        if pkg_metadata is None:
            return False

        if pkg_metadata.autoupdate is None:
            logger.error("Cannot update package because autoupdate field is missing")
            return False

        last_version_from_anytia = await self._get_package_last_version(
            pkg_metadata.autoupdate.upstream_pkg_name,
            pkg_metadata.autoupdate.anytia_backend,
        )
        if last_version_from_anytia is None:
            # TODO: some error or something
            return False

        self._ensure_switching_to_branch(pkg_name)
        specfile = Specfile(f"{self.cwd}/{pkg_name}.spec")
        if parse(last_version_from_anytia) == parse(specfile.expanded_version):
            logger.info(f"Package {pkg_name} has the most recent version")
            return True

        self.pkg_commit_sha[pkg_name] = self._update_version_of_package(
            specfile, last_version_from_anytia, pkg_name
        )
        return await self._push_changes(f"{UPDATE_BRANCH_PREFIX}_{pkg_name}")

    async def _update_all_packages(self) -> int:
        # TODO: use return values for some retry and report logic
        packages = self.local_repo.get_packages("")
        await asyncio.gather(*(self.update_package(pkg) for pkg in packages))
        if not self.pkg_commit_sha:
            return EXIT_SUCCESS

        logger.info("Wait for 30 sec to give packit chance to react")
        sleep(30)
        updated_packages = self.pkg_commit_sha.keys()
        await asyncio.gather(
            *(self.wait_for_check_run_to_end(pkg) for pkg in updated_packages)
        )
        return EXIT_SUCCESS

    def run_autoupdate(self) -> int:
        asyncio.run(self._update_all_packages())
        return EXIT_SUCCESS


if __name__ == "__main__":
    sys.exit(Autoupdator69().run_autoupdate())
