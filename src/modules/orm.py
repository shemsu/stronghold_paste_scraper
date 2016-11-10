import sqlite3

from modules.common import Base


class SQLiteConnection(Base):
    def __init__(self, context):
        super().__init__(context)
        self._connection = None

    @property
    def connection(self):
        if self._connection is None:
            self._connection = sqlite3.connect(self.context.config.DB_FILEPATH)
        return self._connection

    def execute(self, query, params=None, many=False, commit=False, close=False):
        cursor = self.connection.cursor()
        execute_function = cursor.executemany if many else cursor.execute

        self.logger.debug("Executing query: %s", query)
        if params is not None:
            self.logger.debug("With params: %s", params)
        else:
            params = tuple()
        execute_function(query, params)

        if commit:
            self.connection.commit()
        if close:
            self.close()

        return cursor

    def execute_fetch_one_record(self, query, params=None):
        cursor = self.execute(query, params)
        result = cursor.fetchone()
        cursor.close()
        return result

    def execute_fetch_single_value(self, query, params=None):
        cursor = self.execute(query, params)
        result = cursor.fetchone()
        cursor.close()
        return None if not result else result[0]

    @property
    def placeholder(self):
        return '?'

    def close(self):
        if self._connection:
            self._connection.close()
            self._connection = None


class Model(Base):
    """
    Represents a single database table.

    To lessen the possibility of collision with column names,
    all the internal properties and methods are named
    starting with __property__ and __method__ respectfully.

    Every method with its name prefixed with __normalize__ will be run
    each time save() is called.

    Stored names for public access:
        save
        delete
        get_table_name

    Other stored names:
        pk
    """
    NORMALIZATION_PREFIX = '__normalize__'
    ID_KEYWORD = 'pk'

    def __init__(self, context, **kwargs):
        super().__init__(context)
        self.__id = None
        self.__table_name = None
        self.__columns = None
        self.__normalizations = None
        self.__method__validate_inheriting_class()
        self.__method__connect()
        self.__method__create_table_if_necessary()
        self.__method__populate_model_fields(kwargs)
        self.__method__collect_normalization_methods()
        self.__method__normalize()

    def __eq__(self, other):
        for column in self.__property__columns:
            if getattr(self, column) != getattr(other, column):
                return False
        return True

    def __str__(self):
        return ", ".join("{}: {}".format(column, getattr(self, column)) for column in self.__property__columns)

    @property
    def __property__table_name(self):
        if not self.__table_name:
            model_name = type(self).__name__.lower()
            self.__table_name = 'tbl_{}{}'.format(model_name, '' if model_name.endswith('s') else 's')
        return self.__table_name

    @property
    def __property__columns(self):
        if not self.__columns:
            self.__columns = [field_name for field_name, _ in self.FIELDS]
        return self.__columns

    @property
    def __property__values(self):
        return [getattr(self, field_name) for field_name, _ in self.FIELDS]

    def __method__connect(self):
        self.connection = SQLiteConnection(self.context)

    def __method__validate_inheriting_class(self):
        if not self.FIELDS:
            raise NotImplementedError("Inheriting class must provide the FIELDS structure!")

    def __method__collect_normalization_methods(self):
        self.logger.info("Collecting normalization methods")
        self.__normalizations = [getattr(self, method)
                                 for method
                                 in dir(self)
                                 if callable(getattr(self, method))
                                 and getattr(self, method).__name__.startswith(self.NORMALIZATION_PREFIX)]
        self.logger.debug("Collected: %s", self.__normalizations)

    def __method__get_database_field_type(self, field_type):
        database_field_type = 'text'
        if field_type == 'date':
            database_field_type = 'date'
        elif field_type != 'string':
            raise AttributeError("Invalid field type: {}".format(field_type))
        return database_field_type

    def __method__create_table(self, table_name):
        self.logger.info("Creating table %s", table_name)
        query_parts = ["CREATE TABLE {} ".format(table_name)]
        query_parts.append("(")
        columns = ["{} integer primary key".format(self.context.config.DB_ID_FIELD)]
        for field_name, field_type in self.FIELDS:
            columns.append("{} {}".format(field_name, self.__method__get_database_field_type(field_type)))
        query_parts.append(", ".join(columns))
        query_parts.append(")")
        self.connection.execute(query="".join(query_parts), commit=True)

    def __method__create_table_if_necessary(self):
        self.logger.debug("Checking whether table %s has to be created", self.__property__table_name)
        if self.connection.execute_fetch_single_value("SELECT name "
                                                      "FROM sqlite_master "
                                                      "WHERE type={placeholder} "
                                                      "AND name={placeholder}".format(placeholder=self.connection.placeholder),
                                                      ('table', self.__property__table_name)):
            self.logger.debug("Table %s already exists", self.__property__table_name)
        else:
            self.__method__create_table(self.__property__table_name)

    def __method__populate_model_fields(self, kwargs):
        # creating a new model object in case id is not provided
        if self.ID_KEYWORD not in kwargs:
            for field_name in self.__property__columns:
                setattr(self, field_name, kwargs.get(field_name))
            return

        # retrieving the record otherwise
        if self.ID_KEYWORD in kwargs:
            record = self.connection.execute_fetch_one_record("SELECT {} "
                                                              "FROM {} "
                                                              "WHERE {}={}".format(', '.join(self.__property__columns),
                                                                                   self.__property__table_name,
                                                                                   self.context.config.DB_ID_FIELD,
                                                                                   kwargs[self.ID_KEYWORD]))
            if not record:
                raise ValueError("No {} record found with {} = {}".format(type(self).__name__, self.context.config.DB_ID_FIELD, kwargs[self.ID_KEYWORD]))
            record_dict = dict(zip(self.__property__columns, record))
            self.__id = kwargs[self.ID_KEYWORD]
            for field_name, field_value in record_dict.items():
                setattr(self, field_name, field_value)

    def __method__normalize(self):
        self.logger.info("Normalizing the fields")
        for normalization_method in self.__normalizations:
            try:
                normalization_method()
            except Exception as e:
                self.logger.error("%s failed: %s", normalization_method.__name__, e)

    def __method__update(self):
        self.logger.info("Updating a %s record", type(self).__name__)
        value_placeholders = ', '.join("{}={}".format(field_name, self.connection.placeholder) for field_name in self.__property__columns)
        self.connection.execute("UPDATE {} "
                                "SET {} "
                                "WHERE {}={}".format(self.__property__table_name,
                                                     value_placeholders,
                                                     self.context.config.DB_ID_FIELD,
                                                     self.__id),
                                tuple(self.__property__values),
                                commit=True)

    def get_table_name(self):
        return self.__property__table_name

    def save(self):
        """
        Stores the model instance as a new record in the representing table
        """
        if self.__id:
            self.__method__update()
            return

        self.logger.info("Storing a %s record", type(self).__name__)
        placeholders = ('{}, '.format(self.connection.placeholder) * len(self.FIELDS)).strip(', ')
        self.connection.execute("INSERT INTO {} ({}) VALUES ({})".format(self.__property__table_name,
                                                                         ', '.join(self.__property__columns),
                                                                         placeholders),
                                tuple(self.__property__values),
                                commit=True)
        self.__id = self.connection.execute_fetch_single_value("SELECT last_insert_rowid()")

    def delete(self):
        self.logger.info("Deleting a %s record", type(self).__name__)
        self.connection.execute("DELETE FROM {} "
                                "WHERE {}={}".format(self.__property__table_name,
                                                     self.context.config.DB_ID_FIELD,
                                                     self.__id),
                                commit=True)


class ModelCollection(Base):
    def __init__(self, context, model):
        super().__init__(context)
        self.model = model
        self.connection = SQLiteConnection(self.context)

    def get_the_most_recent(self):
        model_template = self.model(self.context)
        latest_id = self.connection.execute_fetch_single_value("SELECT {} "
                                                               "FROM {} "
                                                               "ORDER BY {} DESC "
                                                               "LIMIT 1".format(self.context.config.DB_ID_FIELD,
                                                                                model_template.get_table_name(),
                                                                                self.context.config.DB_ID_FIELD))
        if not latest_id:
            return None

        model_object = self.model(self.context, pk=latest_id)
        return model_object