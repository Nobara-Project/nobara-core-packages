import contextlib
import logging
import queue
import threading
import time
from logging.handlers import QueueHandler
from typing import Any

import gi  # type: ignore[import]
import libdnf5 as dnf  # type: ignore[import]
from libdnf5.common import QueryCmp_NEQ  # type: ignore[import]
from libdnf5.repo import RepoQuery  # type: ignore[import]
from libdnf5.rpm import Package, PackageQuery  # type: ignore[import]

gi.require_version("Gtk", "3.0")

from gi.repository import Gtk  # type: ignore[import]


class AttributeDict(dict[str, Any]):
    def __init__(self, id: str, metalink: Any, mirrorlist: Any, baseurl: Any) -> None:
        super().__init__()
        self.id = id
        self.metalink = metalink
        self.mirrorlist = mirrorlist
        self.baseurl = baseurl

    def __getattr__(self, attr: str) -> Any:
        try:
            return self[attr]
        except KeyError as err:
            raise AttributeError(
                f"'AttributeDict' object has no attribute '{attr}'"
            ) from err

    def __setattr__(self, attr: str, value: Any) -> None:
        self[attr] = value

    def __delattr__(self, attr: str) -> None:
        try:
            del self[attr]
        except KeyError as err:
            raise AttributeError(
                f"'AttributeDict' object has no attribute '{attr}'"
            ) from err


def repoindex(retries: int = 3, delay: int = 5) -> list[AttributeDict]:
    attempt = 0
    logger = logging.getLogger()
    while attempt < retries:
        base = dnf.base.Base()
        try:
            cache_directory = base.get_config().get_cachedir_option().get_value()
            base.get_config().get_system_cachedir_option().set(cache_directory)

            base.load_config()
            base.setup()

            base.repo_sack = base.get_repo_sack()
            base.repo_sack.create_repos_from_system_configuration()

            def reset_backend(base: dnf.base.Base) -> None:
                base.repo_sack = base.get_repo_sack()
                base.repo_sack.create_repos_from_system_configuration()
                try:
                    base.repo_sack.load_repos()  # dnf5 5.2.0
                except Exception:
                    base.repo_sack.update_and_load_enabled_repos(True)  # dnf5 5.1.x

            reset_backend(base)

            def get_enabled_repos(base: dnf.base.Base) -> list[AttributeDict]:
                enabled_repos = []
                for repo in RepoQuery(base):
                    if repo.is_enabled():
                        id = repo.get_id()
                        metalink = None
                        mirrorlist = None
                        baseurl = None

                        with contextlib.suppress(Exception):
                            metalink = repo.get_config().get_metalink_option().get_value()
                        with contextlib.suppress(Exception):
                            mirrorlist = repo.get_config().get_mirrorlist_option().get_value()
                        with contextlib.suppress(Exception):
                            baseurl = repo.get_config().get_baseurl_option().get_value()

                        repo_info = AttributeDict(
                            id=id,
                            metalink=metalink,
                            mirrorlist=mirrorlist,
                            baseurl=baseurl,
                        )
                        enabled_repos.append(repo_info)
                return enabled_repos

            return get_enabled_repos(base)
        except FileNotFoundError as e:
            if e.errno == 2:
                attempt += 1
                logger.info("Attempt %d failed with error: %s. Retrying in %d seconds...", attempt, e, delay)
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            logger.error("An unexpected error occurred: %s", e)
            raise
        finally:
            try:
                del base
            except Exception as e:
                logger.error("Failed to close base: %s", e)
    raise Exception("Failed to complete operation after %d attempts" % retries)


def updatechecker(*, retries: int = 3, delay: int = 5) -> list[str]:
    attempt = 0
    logger = logging.getLogger()
    while attempt < retries:
        base = dnf.base.Base()
        try:
            cache_directory = base.get_config().get_cachedir_option().get_value()
            base.get_config().get_system_cachedir_option().set(cache_directory)

            base.load_config()
            base.setup()

            def reset_backend(base: dnf.base.Base) -> None:
                base.repo_sack = base.get_repo_sack()
                base.repo_sack.create_repos_from_system_configuration()
                try:
                    base.repo_sack.load_repos()  # dnf5 5.2.0
                except Exception:
                    base.repo_sack.update_and_load_enabled_repos(True)  # dnf5 5.1.x

            reset_backend(base)

            def get_repo_priority(repo_name: str, base: dnf.base.Base) -> int:
                repos_query = RepoQuery(base)
                for repo in repos_query:
                    if repo.get_id() == repo_name:
                        return repo.get_priority()
                return 99

            def get_package_repos(package_name: str, base: dnf.base.Base) -> list[str]:
                repos = set()
                query = PackageQuery(base)
                query.filter_name([package_name])
                for pkg in query:
                    repos.add(pkg.get_repo_id())
                return list(repos)

            updates = PackageQuery(base)
            updates.filter_upgrades()
            updates.filter_arch(["src"], QueryCmp_NEQ)
            updates.filter_latest_evr()

            updates_list = list(updates)

            latest_versions: dict[str, Package] = {}
            for pkg in updates_list:
                repos = get_package_repos(pkg.get_name(), base)
                repo_priorities = [get_repo_priority(repo, base) for repo in repos]
                lowest_priority = min(repo_priorities) if repo_priorities else 99
                pkg_repo_priority = get_repo_priority(pkg.get_repo_id(), base)

                if pkg_repo_priority == lowest_priority:
                    if pkg.get_name() in latest_versions:
                        if pkg.get_evr() > latest_versions[pkg.get_name()].get_evr():
                            latest_versions[pkg.get_name()] = pkg
                    else:
                        latest_versions[pkg.get_name()] = pkg

            return [update.get_name() for update in list(latest_versions.values())]
        except FileNotFoundError as e:
            if e.errno == 2:
                attempt += 1
                logger.info("Attempt %d failed with error: %s. Retrying in %d seconds...", attempt, e, delay)
                time.sleep(delay)
            else:
                raise
        except Exception as e:
            logger.error("An unexpected error occurred: %s", e)
            raise
        finally:
            try:
                del base
            except Exception as e:
                logger.error("Failed to close base: %s", e)
    raise Exception("Failed to complete operation after %d attempts" % retries)



class PackageUpdater:
    def __init__(
        self,
        package_names: list[str],
        action: str,
        liststore: Gtk.ListStore,
        logger: logging.Logger | None = None,
    ):
        self.package_names = package_names
        self.liststore = liststore
        self.log_queue: queue.Queue[str] = queue.Queue()
        self.queue_handler = QueueHandler(self.log_queue)
        self.logger = logger if logger is not None else logging.getLogger()
        self.logger.addHandler(self.queue_handler)
        self.logger.setLevel(logging.INFO)
        self.thread = threading.Thread(target=self.update_packages, args=(action,))
        self.thread.start()

    def reset_backend(self, base: dnf.base.Base) -> None:
        base.repo_sack = base.get_repo_sack()
        base.repo_sack.create_repos_from_system_configuration()
        base.repo_sack.update_and_load_enabled_repos(True)

    def update_packages(self, action: str, retries: int = 3, delay: int = 5) -> None:
        attempt = 0
        while attempt < retries:
            if self.package_names:
                try:
                    base = dnf.base.Base()
                    cache_directory = base.get_config().get_cachedir_option().get_value()
                    base.get_config().get_system_cachedir_option().set(cache_directory)

                    base.load_config()
                    base.setup()

                    self.reset_backend(base)
                    self.logger.info("Read all repos")

                    action_log_string = "Upgrading packages:"

                    goal = dnf.base.Goal(base)

                    for package_name in self.package_names:
                        if action == "upgrade":
                            goal.add_rpm_upgrade(package_name)
                        elif action == "install":
                            goal.add_rpm_install(package_name)
                            action_log_string = "Installing packages:"
                        elif action == "remove":
                            goal.add_rpm_remove(package_name)
                            action_log_string = "Removing packages:"

                    self.logger.info(
                        "%s\n%s", action_log_string, chr(10).join(self.package_names)
                    )

                    transaction = goal.resolve()

                    transaction.download()
                    self.logger.info("Downloaded packages")

                    self.logger.info("Starting transaction")
                    transaction.run()
                    self.logger.info("Performed transaction")

                    self.logger.info("Successfully updated packages!")
                    return  # Exit the loop on success
                except FileNotFoundError as e:
                    if e.errno == 2:
                        attempt += 1
                        self.logger.info("Attempt %d failed with error: %s. Retrying in %d seconds...", attempt, e, delay)
                        time.sleep(delay)
                    else:
                        raise
                except Exception as e:
                    self.logger.error("An unexpected error occurred: %s", e)
                    raise
                finally:
                    try:
                        del base
                    except Exception as e:
                        self.logger.error("Failed to close base: %s", e)
        raise Exception("Failed to complete operation after %d attempts" % retries)
