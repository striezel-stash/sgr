import gzip
import logging
import os
from copy import deepcopy
from itertools import islice
from typing import Tuple

import requests
from minio import Minio

import splitgraph.config
from splitgraph.commandline import get_exception_name
from splitgraph.ingestion.csv.common import CSVOptions, get_bool, make_csv_reader
from splitgraph.ingestion.inference import infer_sg_schema

try:
    from multicorn import (
        ForeignDataWrapper,
        ANY,
        TableDefinition,
        ColumnDefinition,
    )
except ImportError:
    # Multicorn not installed (OK if we're not on the engine -- tests).
    ForeignDataWrapper = object
    ANY = object()
    TableDefinition = dict
    ColumnDefinition = dict

try:
    from multicorn.utils import log_to_postgres
except ImportError:
    log_to_postgres = print

_PG_LOGLEVEL = logging.INFO


def _get_table_definition(response, fdw_options, table_name, table_options):
    csv_options, reader = make_csv_reader(response, CSVOptions.from_fdw_options(fdw_options))
    sample = list(islice(reader, 1000))

    if not csv_options.header:
        sample = [[str(i) for i in range(len(sample))]] + sample

    sg_schema = infer_sg_schema(sample, None, None)
    # Build Multicorn TableDefinition. ColumnDefinition takes in type OIDs,
    # typmods and other internal PG stuff but other FDWs seem to get by with just
    # the textual type name.
    return TableDefinition(
        table_name=table_name[len(fdw_options.get("s3_object_prefix", "")) :],
        schema=None,
        columns=[ColumnDefinition(column_name=c.name, type_name=c.pg_type) for c in sg_schema],
        options=table_options,
    )


class CSVForeignDataWrapper(ForeignDataWrapper):
    """Foreign data wrapper for CSV files stored in S3 buckets or HTTP"""

    def can_sort(self, sortkeys):
        # Currently, can't sort on anything. In the future, we can infer which
        # columns a CSV is sorted on and return something more useful here.
        return []

    def get_rel_size(self, quals, columns):
        return 1000000, len(columns) * 10

    def explain(self, quals, columns, sortkeys=None, verbose=False):
        if self.mode == "http":
            return ["HTTP request", f"URL: {self.url}"]
        else:
            return [
                "S3 request",
                f"Endpoint: {self.s3_client._base_url}",
                f"Bucket: {self.s3_bucket}",
                f"Object ID: {self.s3_object}",
            ]

    def _read_csv(self, csv_reader, csv_options):
        header_skipped = False
        for row in csv_reader:
            if not header_skipped and csv_options.header:
                header_skipped = True
                continue
            # CSVs don't really distinguish NULLs and empty strings well. We know
            # that empty strings should be NULLs when coerced into non-strings but we
            # can't easily access type information here. Do a minor hack and treat
            # all empty strings as NULLs.
            row = [r if r != "" else None for r in row]
            yield row

    def execute(self, quals, columns, sortkeys=None):
        """Main Multicorn entry point."""

        if self.mode == "http":
            with requests.get(
                self.url, stream=True, verify=os.environ.get("SSL_CERT_FILE", True)
            ) as response:
                response.raise_for_status()
                stream = response.raw
                if response.headers.get("Content-Encoding") == "gzip":
                    stream = gzip.GzipFile(fileobj=stream)

                csv_options = self.csv_options
                if csv_options.encoding is None and not csv_options.autodetect_encoding:
                    csv_options = csv_options._replace(encoding=response.encoding)

                csv_options, reader = make_csv_reader(stream, csv_options)
                yield from self._read_csv(reader, csv_options)
        else:
            response = None
            try:
                response = self.s3_client.get_object(
                    bucket_name=self.s3_bucket, object_name=self.s3_object
                )
                csv_options = self.csv_options
                if csv_options.encoding is None and not csv_options.autodetect_encoding:
                    csv_options = csv_options._replace(autodetect_encoding=True)
                csv_options, reader = make_csv_reader(response, csv_options)
                yield from self._read_csv(reader, csv_options)
            finally:
                if response:
                    response.close()
                    response.release_conn()

    @classmethod
    def import_schema(cls, schema, srv_options, options, restriction_type, restricts):
        # Implement IMPORT FOREIGN SCHEMA to instead scan an S3 bucket for CSV files
        # and infer their CSV schema.

        # Merge server options and options passed to IMPORT FOREIGN SCHEMA
        fdw_options = deepcopy(srv_options)
        for k, v in options.items():
            fdw_options[k] = v

        if fdw_options.get("url"):
            # Infer from HTTP -- singular table with name "data"
            with requests.get(
                fdw_options["url"], stream=True, verify=os.environ.get("SSL_CERT_FILE", True)
            ) as response:
                response.raise_for_status()
                stream = response.raw
                if response.headers.get("Content-Encoding") == "gzip":
                    stream = gzip.GzipFile(fileobj=stream)
                return [_get_table_definition(stream, fdw_options, "data", None)]

        # Get S3 options
        client, bucket, prefix = cls._get_s3_params(fdw_options)

        # Note that we ignore the "schema" here (e.g. IMPORT FOREIGN SCHEMA some_schema)
        # and take all interesting parameters through FDW options.

        # Allow just introspecting one object
        if "s3_object" in fdw_options:
            objects = [fdw_options["s3_object"]]
        else:
            objects = [
                o.object_name
                for o in client.list_objects(
                    bucket_name=bucket, prefix=prefix or None, recursive=True
                )
            ]

        result = []

        for o in objects:
            if restriction_type == "limit" and o not in restricts:
                continue

            if restriction_type == "except" and o in restricts:
                continue

            response = None
            try:
                response = client.get_object(bucket, o)
                result.append(
                    _get_table_definition(
                        response,
                        fdw_options,
                        o,
                        {"s3_object": o},
                    )
                )
            except Exception as e:
                logging.error(
                    "Error scanning object %s, ignoring: %s: %s", o, get_exception_name(e), e
                )
                log_to_postgres(
                    "Error scanning object %s, ignoring: %s: %s" % (o, get_exception_name(e), e)
                )
            finally:
                if response:
                    response.close()
                    response.release_conn()

        return result

    @classmethod
    def _get_s3_params(cls, fdw_options) -> Tuple[Minio, str, str]:
        s3_client = Minio(
            endpoint=fdw_options["s3_endpoint"],
            access_key=fdw_options.get("s3_access_key"),
            secret_key=fdw_options.get("s3_secret_key"),
            secure=get_bool(fdw_options, "s3_secure"),
            region=fdw_options.get("s3_region"),
        )

        s3_bucket = fdw_options["s3_bucket"]

        # We split the object into a prefix + object ID to let us mount a bunch of objects
        # with the same prefix as CSV files.
        s3_object_prefix = fdw_options.get("s3_object_prefix", "")

        return s3_client, s3_bucket, s3_object_prefix

    def __init__(self, fdw_options, fdw_columns):
        # Initialize the logger that will log to the engine's stderr: log timestamp and PID.

        logging.basicConfig(
            format="%(asctime)s [%(process)d] %(levelname)s %(message)s",
            level=splitgraph.config.CONFIG["SG_LOGLEVEL"],
        )

        # Dict of connection parameters
        self.fdw_options = fdw_options

        # The foreign datawrapper columns (name -> ColumnDefinition).
        self.fdw_columns = fdw_columns

        self.csv_options = CSVOptions.from_fdw_options(fdw_options)

        # For HTTP: use full URL
        if fdw_options.get("url"):
            self.mode = "http"
            self.url = fdw_options["url"]
        else:
            self.mode = "s3"
            self.s3_client, self.s3_bucket, self.s3_object_prefix = self._get_s3_params(fdw_options)

            # TODO need a way to pass the table params (e.g. the actual S3 object name which
            #  might be different from table) into the preview and return it from introspection.
            self.s3_object = fdw_options["s3_object"]
