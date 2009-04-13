import logging

from sqlalchemy import Column, Integer, Unicode, DateTime, ForeignKey, UniqueConstraint, MetaData, Table, PassiveDefault, __version__
from sqlalchemy.orm import relation
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.sql import func
from sqlalchemy.sql.expression import text
from sqlalchemy.exceptions import OperationalError, InvalidRequestError

if __version__ < '0.5':
    NoResultFound = InvalidRequestError
else:
    from sqlalchemy.orm.exc import NoResultFound

metadata = MetaData()
Base = declarative_base(metadata=metadata)
log = logging.getLogger('ibid.models')

class VersionedSchema(object):
    """For an initial table schema, set
    table.versioned_schema = VersionedSchema(__table__, 1)
    Table creation (upgrading to version 1) is implicitly supported.

    When you have upgrades to the schema, instead of using VersionedSchema
    directly, derive from it and include your own upgrade_x_to_y(self) methods,
    where y = x + 1
    
    In the upgrade methods, you can call the helper functions:
    add_column, drop_column, rename_column, alter_column
    They try to do the correct thing in most situations, including rebuilding
    tables in SQLite, which doesn't actually support dropping/altering columns.
    For column parameters, while you can point to columns in the table
    definition, it is better style to repeat the Column() specification as the
    column might be altered in a future version.
    """

    def __init__(self, table, version):
        self.table = table
        self.version = version

    def is_up_to_date(self, session):
        "Is the table in the database up to date with the schema?"

        if not session.bind.has_table(self.table.name):
            return False

        try:
            schema = session.query(Schema).filter(Schema.table==unicode(self.table.name)).one()
            return schema.version == self.version
        except NoResultFound:
            return False

    def upgrade_schema(self, sessionmaker):
        "Upgrade the table's schema to the latest version."

        for fk in self.table.foreign_keys:
            dependancy = fk.target_fullname.split('.')[0]
            log.debug("Upgrading table %s before %s", dependancy, self.table.name)
            metadata.tables[dependancy].versioned_schema.upgrade_schema(sessionmaker)

        self.upgrade_session = session = sessionmaker()
        trans = session.begin()

        schema = session.query(Schema).filter(Schema.table==unicode(self.table.name)).first()

        try:
            if not schema:
                log.info(u"Creating table %s", self.table.name)

                # If MySQL, we prefer InnoDB:
                if 'mysql_engine' not in self.table.kwargs:
                    self.table.kwargs['mysql_engine'] = 'InnoDB'

                self.table.create(bind=session.bind)

                schema = Schema(unicode(self.table.name), self.version)
                session.save_or_update(schema)

            elif self.version > schema.version:
                self.upgrade_reflected_model = MetaData(session.bind, reflect=True)
                for version in range(schema.version + 1, self.version + 1):
                    log.info(u"Upgrading table %s to version %i", self.table.name, version)

                    trans.commit()
                    trans = session.begin()

                    eval('self.upgrade_%i_to_%i' % (version - 1, version))()

                    schema.version = version
                    session.save_or_update(schema)
                del self.upgrade_reflected_model

            trans.commit()

        except:
            trans.rollback()
            raise

        session.close()
        del self.upgrade_session

    def get_reflected_model(self):
        "Get a reflected table from the current DB's schema"

        return self.upgrade_reflected_model.tables.get(self.table.name, None)

    def add_column(self, col):
        "Add column col to table"

        session = self.upgrade_session
        table = self.get_reflected_model()

        log.debug(u"Adding column %s to table %s", col.name, table.name)

        table.append_column(col)

        sg = session.bind.dialect.schemagenerator(session.bind.dialect, session.bind)
        description = sg.get_column_specification(col)

        session.execute('ALTER TABLE "%s" ADD COLUMN %s;' % (table.name, description))

    def drop_column(self, col_name):
        "Drop column col_name from table"

        session = self.upgrade_session

        log.debug(u"Dropping column %s from table %s", col_name, self.table.name)

        if session.bind.dialect.name == 'sqlite':
            self.rebuild_sqlite({col_name: None})
        else:
            session.execute('ALTER TABLE "%s" DROP COLUMN "%s";' % (self.table.name, col_name))

    def rename_column(self, col, old_name):
        "Rename column from old_name to Column col"

        session = self.upgrade_session
        table = self.get_reflected_model()

        log.debug(u"Rename column %s to %s in table %s", old_name, col.name, table.name)

        if session.bind.dialect.name == 'sqlite':
            self.rebuild_sqlite({old_name: col})
        elif session.bind.dialect.name == 'mysql':
            self.alter_column(col, old_name)
        else:
            session.execute('ALTER TABLE "%s" RENAME COLUMN "%s" TO "%s";' % (table.name, old_name, col.name))

    def alter_column(self, col, old_name=None, length_only=False):
        """Change a column (possibly renaming from old_name) to Column col.
        Specify length_only if the change is simply a change of data-type length."""

        session = self.upgrade_session
        table = self.get_reflected_model()

        log.debug(u"Altering column %s in table %s", col.name, table.name)

        sg = session.bind.dialect.schemagenerator(session.bind.dialect, session.bind)
        description = sg.get_column_specification(col)

        if session.bind.dialect.name == 'sqlite':
            #TODO: Automatically detect length_only
            if length_only:
                # SQLite doesn't enforce value length restrictions, only type changes have a real effect
                return

            self.rebuild_sqlite({old_name is None and col.name or old_name: col})

        elif session.bind.dialect.name == 'mysql':
            session.execute('ALTER TABLE "%s" CHANGE "%s" %s;'
                % (table.name, old_name is not None and old_name or col.name, description))

        else:
            if old_name is not None:
                self.rename_column(col, old_name)
            session.execute('ALTER TABLE "%s" ALTER COLUMN "%s" TYPE %s'
                % (table.name, col.name, description.split(" ", 1)[1]))

    def rebuild_sqlite(self, colmap):
        """SQLite doesn't support modification of table schema - must rebuild the table.
        colmap maps old column names to new Columns (or None for column deletion).
        Only modified columns need to be listed, unchaged columns are carried over automatically.
        Specify table in case name has changed in a more recent version."""

        session = self.upgrade_session
        table = self.get_reflected_model()

        log.debug(u"Rebuilding SQLite table %s", table.name)

        fullcolmap = {}
        for col in table.c:
            if col.name in colmap:
                if colmap[col.name] is not None:
                    fullcolmap[col.name] = colmap[col.name].name
            else:
                fullcolmap[col.name] = col.name

        for old, col in colmap.iteritems():
            del table.c[old]
            if col is not None:
                table.append_column(col)

        session.execute('ALTER TABLE "%s" RENAME TO "%s_old";' % (table.name, table.name))
        table.create()
        session.execute('INSERT INTO "%s" ("%s") SELECT "%s" FROM "%s_old";'
                % (table.name, '", "'.join(fullcolmap.values()), '", "'.join(fullcolmap.keys()), table.name))
        session.execute('DROP TABLE "%s_old";' % table.name)

class Schema(Base):
    __table__ = Table('schema', Base.metadata,
        Column('id', Integer, primary_key=True),
        Column('table', Unicode(32), unique=True, nullable=False),
        Column('version', Integer, nullable=False),
        useexisting=True)

    # Upgrades to this table are probably going to be tricky
    class SchemaSchema(VersionedSchema):
        def upgrade_schema(self, sessionmaker):
            session = sessionmaker()

            if not session.bind.has_table(self.table.name):
                metadata.bind = session.bind
                self.table.kwargs['mysql_engine'] = 'InnoDB'
                self.table.create()

                schema = Schema(unicode(self.table.name), self.version)
                session.save_or_update(schema)

            session.flush()
            session.close()

    __table__.versioned_schema = SchemaSchema(__table__, 1)
    
    def __init__(self, table, version=0):
        self.table = table
        self.version = version

    def __repr__(self):
        return '<Schema %s>' % self.table

class Identity(Base):
    __table__ = Table('identities', Base.metadata,
        Column('id', Integer, primary_key=True),
        Column('account_id', Integer, ForeignKey('accounts.id')),
        Column('source', Unicode(16), nullable=False),
        Column('identity', Unicode(64), nullable=False),
        Column('created', DateTime, default=func.current_timestamp()),
        UniqueConstraint('source', 'identity'),
        useexisting=True)

    __table__.versioned_schema = VersionedSchema(__table__, 1)

    def __init__(self, source, identity, account_id=None):
        self.source = source
        self.identity = identity
        self.account_id = account_id

    def __repr__(self):
        return '<Identity %s on %s>' % (self.identity, self.source)

class Attribute(Base):
    __table__ = Table('account_attributes', Base.metadata,
        Column('id', Integer, primary_key=True),
        Column('account_id', Integer, ForeignKey('accounts.id'), nullable=False),
        Column('name', Unicode(32), nullable=False),
        Column('value', Unicode(128), nullable=False),
        UniqueConstraint('account_id', 'name'),
        useexisting=True)

    __table__.versioned_schema = VersionedSchema(__table__, 1)

    def __init__(self, name, value):
        self.name = name
        self.value = value

    def __repr__(self):
        return '<Attribute %s = %s>' % (self.name, self.value)

class Credential(Base):
    __table__ = Table('credentials', Base.metadata,
        Column('id', Integer, primary_key=True),
        Column('account_id', Integer, ForeignKey('accounts.id'), nullable=False),
        Column('source', Unicode(16)),
        Column('method', Unicode(16), nullable=False),
        Column('credential', Unicode(256), nullable=False),
        useexisting=True)

    __table__.versioned_schema = VersionedSchema(__table__, 1)

    def __init__(self, method, credential, source=None, account_id=None):
        self.account_id = account_id
        self.source = source
        self.method = method
        self.credential = credential

class Permission(Base):
    __table__ = Table('permissions', Base.metadata,
        Column('id', Integer, primary_key=True),
        Column('account_id', Integer, ForeignKey('accounts.id'), nullable=False),
        Column('name', Unicode(16), nullable=False),
        Column('value', Unicode(4), nullable=False),
        UniqueConstraint('account_id', 'name'),
        useexisting=True)

    __table__.versioned_schema = VersionedSchema(__table__, 1)

    def __init__(self, name=None, value=None, account_id=None):
        self.account_id = account_id
        self.name = name
        self.value = value

class Account(Base):
    __table__ = Table('accounts', Base.metadata,
        Column('id', Integer, primary_key=True),
        Column('username', Unicode(32), unique=True, nullable=False),
        useexisting=True)

    __table__.versioned_schema = VersionedSchema(__table__, 1)

    identities = relation(Identity, backref='account')
    attributes = relation(Attribute)
    permissions = relation(Permission)
    credentials = relation(Credential)

    def __init__(self, username):
        self.username = username

    def __repr__(self):
        return '<Account %s>' % self.username

def check_schema_versions(sessionmaker):
    """Pass through all tables, log out of date ones,
    and except if not all up to date"""

    session = sessionmaker()
    upgrades = []
    for table in metadata.tables.itervalues():
        if not hasattr(table, 'versioned_schema'):
            log.error("Table %s is not versioned.", table.name)
            continue

        if not table.versioned_schema.is_up_to_date(session):
            upgrades.append(table.name)

    if not upgrades:
        return

    raise Exception(u"Tables %s are out of date. Run ibid-setup" % u", ".join(upgrades))

def upgrade_schemas(sessionmaker):
    "Pass through all tables and update schemas"

    # Make sure schema table is created first
    metadata.tables['schema'].versioned_schema.upgrade_schema(sessionmaker)

    for table in metadata.tables.itervalues():
        if not hasattr(table, 'versioned_schema'):
            log.error("Table %s is not versioned.", table.name)
            continue

        table.versioned_schema.upgrade_schema(sessionmaker)

# vi: set et sta sw=4 ts=4:
