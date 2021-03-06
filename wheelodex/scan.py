""" Functions for scanning PyPI for wheels to register """

import logging
from   .dbutil   import add_orphan_wheel, add_project, add_version, add_wheel, \
                            remove_project, remove_version, remove_wheel, \
                            set_serial
from   .pypi_api import PyPIAPI
from   .util     import latest_version

log = logging.getLogger(__name__)

def scan_pypi():
    """
    Use PyPI's XML-RPC and JSON APIs to find & register all wheels for the
    latest version of every project on PyPI.  The database's serial ID is also
    set to PyPI's current value as of the start of the function.

    This function requires a Flask application context with a database
    connection to be in effect.

    .. warning::

        There is a potential for Wheelodex's database to get out of sync with
        PyPI if `scan_pypi()` is run when one or more "remove" actions have
        transpired on PyPI since the last-seen changelog event.  It is
        recommended to only call this function either when no calls to
        `scan_pypi()` or `scan_changelog()` have been made yet or immediately
        after a call to `scan_changelog()`.
    """
    log.info('BEGIN scan_pypi')
    pypi = PyPIAPI()
    serial = pypi.changelog_last_serial()
    log.info('changlog_last_serial() = %d', serial)
    set_serial(serial)
    for pkg in pypi.list_packages():
        log.info('Adding wheels for project %r', pkg)
        project = add_project(pkg)
        data = pypi.project_data(pkg)
        if data is None or not data.get("releases", {}):
            log.info('Project has no releases')
            continue
        versions = list(data["releases"].keys())
        log.debug('Available versions: %r', versions)
        latest = latest_version(versions)
        log.info('Using latest version: %r', latest)
        qty_queued = 0
        vobj = add_version(project, latest)
        for asset in data["releases"][latest]:
            if not asset["filename"].endswith('.whl'):
                log.debug('Asset %s: not a wheel; skipping', asset["filename"])
            else:
                log.debug('Asset %s: adding', asset["filename"])
                qty_queued += 1
                add_wheel(
                    version  = vobj,
                    filename = asset["filename"],
                    url      = asset["url"],
                    size     = asset["size"],
                    md5      = asset["digests"]["md5"].lower(),
                    sha256   = asset["digests"]["sha256"].lower(),
                    uploaded = str(asset["upload_time"]),
                )
        log.info('%s: %d wheels added', pkg, qty_queued)
    log.info('END scan_pypi')

def scan_changelog(since):
    """
    Use PyPI's XML-RPC and JSON APIs to update the wheel registry based on all
    events that have happened on PyPI since serial ID ``since``.  The
    database's serial ID is also set to PyPI's current value as of the start of
    the function.

    This function requires a Flask application context with a database
    connection to be in effect.
    """
    log.info('BEGIN scan_changelog(%d)', since)
    pypi = PyPIAPI()
    for proj, rel, ts, action, serial in pypi.changelog_since_serial(since):
        actwords = action.split()

        # As of pypa/warehouse revision 97f28df (2018-09-20), the possible
        # "action" strings are (found by searching for "JournalEntry" in the
        # code):
        # - "add {python_version} file {filename}"
        # - "remove file {filename}"
        # - "create" [new project]
        # - "remove project"
        # - "new release"
        # - "remove release"
        # - "add Owner {username}"
        # - "add {role_name} {username}"
        # - "remove {role_name} {username}"
        # - "change {role_name} {username} to {role_name2}" [?]
        # - "nuke user"
        # - "docdestroy"

        if actwords[0] == 'add' and len(actwords) == 4 and \
                actwords[2] == 'file' and actwords[3].endswith('.whl'):
            filename = actwords[3]
            log.info('Event %d: wheel %s added', serial, filename)
            # New wheels should more often than not belong to the latest
            # version of the project, and if they don't, they can be pruned out
            # later.  There's likely little to nothing to be gained by
            # comparing `rel` to the latest version in the database at this
            # point.
            v = add_version(proj, rel)
            data = pypi.asset_data(proj, rel, filename)
            if data is not None:
                log.info('Asset %s: adding', filename)
                add_wheel(
                    version  = v,
                    filename = data["filename"],
                    url      = data["url"],
                    size     = data["size"],
                    md5      = data["digests"].get("md5").lower(),
                    sha256   = data["digests"].get("sha256").lower(),
                    uploaded = str(data["upload_time"]),
                )
            else:
                log.info('Asset %s not found in JSON API; will check later',
                         filename)
                add_orphan_wheel(v, filename, ts)

        elif actwords[:2] == ['remove', 'file'] and len(actwords) == 3 and \
                actwords[2].endswith('.whl'):
            log.info('Event %d: wheel %s removed', serial, actwords[2])
            remove_wheel(actwords[2])

        elif action == 'create':
            log.info('Event %d: project %r created', serial, proj)
            add_project(proj)

        elif action == 'remove project':
            log.info('Event %d: project %r removed', serial, proj)
            remove_project(proj)

        elif action == 'new release':
            log.info('Event %d: version %r of project %r released', serial,
                     rel, proj)
            add_version(proj, rel)

        elif action == 'remove release':
            log.info('Event %d: version %r of project %r removed', serial,
                     rel, proj)
            remove_version(proj, rel)

        else:
            log.debug('Event %d: %r: ignoring', serial, action)

        set_serial(serial)
    log.info('END scan_changelog')
