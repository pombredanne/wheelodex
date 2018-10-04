from   contextlib      import contextmanager
from   datetime        import datetime, timezone
import logging
from   typing          import Optional, Union
from   packaging.utils import canonicalize_name as normalize, \
                                canonicalize_version as normversion
from   .models         import OrphanWheel, Project, PyPISerial, Version, \
                                Wheel, db
from   .util           import version_sort_key, wheel_sort_key

log = logging.getLogger(__name__)

@contextmanager
def dbcontext():
    try:
        yield db.session
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    finally:
        db.session.close()

def get_serial() -> Optional[int]:
    """ Returns the serial ID of the last seen PyPI event """
    ps = PyPISerial.query.one_or_none()
    return ps and ps.serial

def set_serial(value: int):
    ps = PyPISerial.query.one_or_none()
    if ps is None:
        db.session.add(PyPISerial(serial=value))
    else:
        ps.serial = max(ps.serial, value)

def add_wheel(version: 'Version', filename, url, size, md5, sha256, uploaded):
    whl = Wheel.query.filter(Wheel.filename == filename).one_or_none()
    if whl is None:
        whl = Wheel(
            version  = version,
            filename = filename,
            url      = url,
            size     = size,
            md5      = md5,
            sha256   = sha256,
            uploaded = uploaded,
        )
        db.session.add(whl)
        for i,w in enumerate(
            ### TODO: Is `version.wheels` safe to use when some of its elements
            ### may have been deleted earlier in the transaction?
            sorted(version.wheels, key=lambda x: wheel_sort_key(x.filename))
        ):
            w.ordering = i
    return whl

def iterqueue(max_wheel_size=None) -> [Wheel]:
    """
    Returns a list of all wheels with neither data nor errors for the latest
    version of each project

    :param int max_wheel_size: If set, only wheels this size or smaller are
        returned
    """
    subq = db.session.query(
        Project.id,
        db.func.max(Version.ordering).label('max_order'),
    ).join(Version).group_by(Project.id).subquery()
    q = Wheel.query.join(Version)\
                   .join(Project)\
                   .join(subq, (Project.id == subq.c.id)
                            & (Version.ordering == subq.c.max_order))\
                   .filter(~Wheel.data.has())\
                   .filter(~Wheel.errors.any())
    if max_wheel_size is not None:
        q = q.filter(Wheel.size <= max_wheel_size)
    ### TODO: Would leaving off the ".all()" give an iterable that plays well
    ### with wheels being given data concurrently?
    return q.all()

def remove_wheel(filename: str):
    Wheel.query.filter(Wheel.filename == filename).delete()
    OrphanWheel.query.filter(OrphanWheel.filename == filename).delete()

def add_project(name: str):
    """
    Create a `Project` with the given name and return it.  If there already
    exists a project with the same name (after normalization), do nothing and
    return that instead.
    """
    return Project.from_name(name)

def get_project(name: str):
    return Project.query.filter(Project.name == normalize(name)).one_or_none()

def remove_project(project: str):
    # This deletes the project's versions (and thus also wheels) but leaves
    # the project entry in place in case it's still referenced as a
    # dependency of other wheels.
    #
    # Note that this filters by PyPI project, not by wheel filename
    # project, as this method is meant to be called in response to "remove"
    # events in the PyPI changelog.
    ### TODO: Look into doing this as a JOIN + DELETE of some sort
    p = get_project(project)
    if p is not None:
        Version.query.filter(Version.project == p).delete()

def add_version(project: Union[str, 'Project'], version: str):
    """
    Create a `Version` with the given project & version string and return it.
    If there already exists a version with the same details, do nothing and
    return that instead.
    """
    if isinstance(project, str):
        project = add_project(project)
    vnorm = normversion(version)
    v = Version.query.filter(Version.project == project)\
                     .filter(Version.name == vnorm)\
                     .one_or_none()
    if v is None:
        v = Version(project=project, name=vnorm, display_name=version)
        db.session.add(v)
        for i,u in enumerate(
            ### TODO: Is `project.versions` safe to use when some of its
            ### elements may have been deleted earlier in the transaction?
            sorted(project.versions, key=lambda x: version_sort_key(x.name))
        ):
            u.ordering = i
    return v

def get_version(project: Union[str, 'Project'], version: str):
    if isinstance(project, str):
        project = get_project(project)
    if project is None:
        return None
    return Version.query.filter(Version.project == project)\
                        .filter(Version.name == normversion(version))\
                        .one_or_none()

def remove_version(project: str, version: str):
    # Note that this filters by PyPI project & version, not by wheel
    # filename project & version, as this method is meant to be called in
    # response to "remove" events in the PyPI changelog.
    ### TODO: Look into doing this as a JOIN + DELETE of some sort
    p = get_project(project)
    if p is not None:
        Version.query.filter(Version.project == p)\
                     .filter(Version.name == normversion(version))\
                     .delete()

def purge_old_versions():
    """
    For each project, keep (a) the latest version, (b) the latest version with
    wheels registered, and (c) the latest version with wheel data, and delete
    all other versions.
    """
    log.info('BEGIN purge_old_versions')
    for p in Project.query.join(Version)\
                          .group_by(Project)\
                          .having(db.func.count(Version.id) > 1):
        latest = p.latest_version
        log.info('Project %s: keeping latest version: %s',
                 p.display_name, latest.display_name)
        latest_wheel = Version.query.with_parent(p)\
                                    .filter(Version.wheels.any())\
                                    .order_by(Version.ordering.desc())\
                                    .first()
        if latest_wheel is not None:
            log.info('Project %s: keeping latest version with wheels: %s',
                     p.display_name, latest_wheel.display_name)
            latest_data = Version.query.with_parent(p)\
                                       .filter(Version.wheels.any(
                                           Wheel.data.has()
                                       ))\
                                       .order_by(Version.ordering.desc())\
                                       .first()
            if latest_data is not None:
                log.info('Project %s: keeping latest version with data: %s',
                         p.display_name, latest_wheel.display_name)
        else:
            latest_data = None
        for v in p.versions:
            if v not in (latest, latest_wheel, latest_data):
                log.info('Project %s: deleting version %s',
                         p.display_name, v.display_name)
                db.session.delete(v)
    log.info('END purge_old_versions')

def add_orphan_wheel(version, filename, uploaded_epoch):
    uploaded = datetime.fromtimestamp(uploaded_epoch, timezone.utc)
    whl = OrphanWheel.query.filter(Wheel.filename == filename).one_or_none()
    if whl is None:
        whl = OrphanWheel(
            version  = version,
            filename = filename,
            uploaded = uploaded,
        )
        db.session.add(whl)
    else:
        # If they keep uploading the wheel, keep checking the JSON API for it.
        whl.uploaded = uploaded
