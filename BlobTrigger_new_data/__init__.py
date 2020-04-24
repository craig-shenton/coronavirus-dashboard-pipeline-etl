#!/usr/bin python3

"""
Coronavirus (COVID-19) in the UK - Dashboard service
====================================================

Consumer data pipeline ETL
--------------------------

ETL service to create consumer-ready CSV and JSON files for download.

The service is dispatched by an event that is triggered every time
a new data file is deployed to the ``publicdata`` blob storage.

Data are identical to the original source, but enjoys a different structure.

.. Note::
    There are missing values in the data. The approach is to leave them
    as blank in the CSV file, and assign a ``null`` value in JSON to
    ensure a consistent structure.


Author:        Pouria Hadjibagheri <pouria.hadjibagheri@phe.gov.uk>
Created:       17 Apr 2020
License:       MIT
Contributors:  Pouria Hadjibagheri
"""

# Imports
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Python:
from typing import (
    NoReturn, Tuple, Optional, TypedDict,
    NamedTuple, Iterable, Union
)
from json import loads, dumps
from sys import exit as sys_exit
import logging
from collections import UserDict

# 3rd party:
from azure.functions import Out, Context

from pandas import DataFrame, to_datetime
from pandas import json_normalize

from datetime import datetime

# Internal:
# None

# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
# Header
__author__ = "Pouria Hadjibagheri"
__copyright__ = "Copyright (c) 2020, Public Health England"
__license__ = "MIT"
__version__ = "0.5.7"
# ~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

VALUE_COLUMNS = (
    'dailyTotalDeaths',
    'dailyDeaths',
    'dailyConfirmedCases',
    'dailyTotalConfirmedCases'
)

CATEGORY_LABELS = (
    "overview",
    "countries",
    "regions",
    "utlas"
)

CASES = "cases"
DEATHS = "deaths"

DATE_COLUMN = "date"

SORT_OUTPUT_BY = ["date", "Area type", "Area name"]

DAILY_RECORD_LABELS = {
    "totalCases": "totalLabConfirmedCases",
    "newCases": "dailyLabConfirmedCases",
}

REPLACEMENT_COLUMNS = {
    "csv": {
        CASES: {
            "date": "Specimen date",
            "dailyConfirmedCases": "Daily lab-confirmed cases",
            "dailyTotalConfirmedCases": "Cumulative lab-confirmed cases"
        },
        DEATHS: {
            "date": "Reporting date",
            "dailyDeaths": "Daily hospital deaths",
            "dailyTotalDeaths": "Cumulative hospital deaths"
        },
    },
    "json": {
        CASES: {
            "Area name": "areaName",
            "Area code": "areaCode",
            "Area type": "areaType",
            "date": "specimenDate",
            "dailyConfirmedCases": "dailyLabConfirmedCases",
            "dailyTotalConfirmedCases": "totalLabConfirmedCases"
        },
        DEATHS: {
            "Area name": "areaName",
            "Area code": "areaCode",
            "Area type": "areaType",
            "date": "reportingDate",
            "dailyDeaths": "dailyHospitalDeaths",
            "dailyTotalDeaths": "totalHospitalDeaths"
        }
    }
}

CRITERIA = {
    CASES: [
        dict(
            by="regions",
            numeric_columns=['dailyConfirmedCases', 'dailyTotalConfirmedCases'],
            area_type="Region",
            area_names_excluded=["Scotland", "Wales", "Northern Ireland", "United Kingdom"]
        ),
        dict(
            by="utlas",
            numeric_columns=['dailyConfirmedCases', 'dailyTotalConfirmedCases'],
            area_type="Upper tier local authority"
        ),
        dict(
            by="countries",
            numeric_columns=['dailyConfirmedCases', 'dailyTotalConfirmedCases'],
            area_type="Nation",
            area_names_excluded=["Scotland", "Wales", "Northern Ireland", "United Kingdom"]
        ),
    ],
    DEATHS: [
        dict(
            by="countries",
            numeric_columns=['dailyDeaths', 'dailyTotalDeaths'],
            area_type="Nation",
            area_names_included=["England", "Scotland", "Northern Ireland", "Wales"]
        ),
        dict(
            by="overview",
            numeric_columns=['dailyDeaths', 'dailyTotalDeaths'],
            area_type="UK",
            area_names_included=["United Kingdom",]
        )
    ]
}

COLUMNS_BY_OUTPUT = {
    DEATHS: dict(
        included_cols=[
            "areaCode",
            "areaType",
            "reportingDate",
            "dailyHospitalDeaths",
            "totalHospitalDeaths"
        ],
        numeric_cols=['dailyHospitalDeaths', 'totalHospitalDeaths']
    ),
    CASES: dict(
        included_cols=[
            "areaCode",
            "areaType",
            "specimenDate",
            "dailyLabConfirmedCases",
            "totalLabConfirmedCases"
        ],
        numeric_cols=["dailyLabConfirmedCases", "totalLabConfirmedCases"]
    )
}

JSON_GROUP_NAME_REPLACEMENTS = {
    "Nation": "countries",
    "UK": "overview",
    "Region": "regions",
    "Upper tier local authority": "utlas"
}


class Metadata(TypedDict):
    lastUpdatedAt: str
    disclaimer: str


class DailyRecords(TypedDict):
    areaName: str
    totalLabConfirmedCases: Union[None, str]
    dailyLabConfirmedCases: Union[None, str]


class InternalProcessor(NamedTuple):
    csv: str
    json: str


class ExtraJsonData(UserDict):
    metadata: Metadata
    dailyRecords: DailyRecords


class GeneralProcessor(NamedTuple):
    data: DataFrame
    json_extras: ExtraJsonData


def produce_json(data: DataFrame, json_extras: ExtraJsonData,
                 numeric_columns: Iterable[str], included_columns: Iterable[str]) -> str:
    """
    Produces a JSON output from the structured data.

    The output is grouped by area type, and formatted as follows:

        {
            "metadata": {
                "lastUpdatedAt": "<ISO FORMATTED DATE>",
                "disclaimer": "<DISCLAIMER MESSAGE>"
            },
            "regions": ["<...>"],
            "utlas": ["<...>"],
            "countries": ["<...>"],
            "overview": ["<...>"],
        }

    Parameters
    ----------
    data: DataFrame
        Structured data that is both sorted and filtered to include only
        the data that is needed in the output.

    json_extras: ExtraJsonData
        Extra data to be included in the JSON file.

    numeric_columns: Iterable[str]
        Numeric columns.

        .. Note::
            These columns are presumed to contain only integer number
            or ``NaN`` values.

    included_columns: Iterable[str]
        Columns to be included in the output.

    Returns
    -------
    str
        JSON output as a string object.
    """
    # JSON output is structured by area type.
    df_by_area = data.groupby("areaType")

    js = dict()
    js.update(json_extras)

    # Adding items by groups (categories, defined as `areaType`):
    for group in df_by_area.groups:
        group_values = loads(df_by_area.get_group(group)[included_columns].to_json(orient='records'))
        # Values default to float because of missing numbers.
        # There's no easy / clean way to convert them onto int.
        for index, value in enumerate(group_values):
            # NOTE: The `value` is an alias (link) to a item in `group_values`,
            # and any alterations to it will be implemented in the original
            # item in `group_values`.
            for col in numeric_columns:
                if value[col] is not None:
                    value[col] = int(value[col])

        js.update({
            # Converting category name the attribute names
            # for JSON output.
            JSON_GROUP_NAME_REPLACEMENTS[group]: group_values
        })

    json_file = dumps(js)

    logging.info(">> JSON file generated.")

    return json_file


def extract_data(data: DataFrame, by: str, numeric_columns: Tuple[str], area_type: str,
                 area_names_excluded: Optional[Tuple[str]] = tuple(),
                 area_names_included: Optional[Tuple[str]] = tuple()) -> DataFrame:
    """
    Extracts data base on "Area type" and other criteria.

    Parameters
    ----------
    data: DataFrame
        Processed data.

    by: str
        Category, whose data is to be included in the output. Must be
        one of ``CATEGORY_LABELS``.

    numeric_columns: Tuple[str]
        Name of numeric columns to be included in the output. Must be
        a subset of ``VALUE_COLUMNS``.

    area_type: str
        Name to be used for the "Area type" column. It provides an
        alternative and user readable name for ``by``.

    area_names_excluded: Optional[Tuple[str]]
        Only include area names that have certain values.

    area_names_included: Optional[Tuple[str]]
        Only include area names that do not include certain values.

    Returns
    -------
    DataFrame
        Extracted data, processed and structured based on the requirements.
    """
    if by not in CATEGORY_LABELS:
        raise ValueError("Value of `by` is not included in `CATEGORY_LABELS`.")

    if not set(numeric_columns).issubset(VALUE_COLUMNS):
        raise ValueError("Value of `numeric_columns` is not a subset of `VALUE_COLUMNS`.")

    # Create column names
    hierarchical_cols = [("value", by, col) for col in numeric_columns]

    # Extract data.
    dd = data.loc[
        (data.loc[:, hierarchical_cols].any(axis=1)),  # exclude empty rows.
        [
            ('areaName', '', ''),
            ('areaCode', '', ''),
            ('date', '', ''),
            *hierarchical_cols
        ]
    ]

    # Apply negative slicing
    if len(area_names_excluded):
        dd = dd.loc[(~dd[('areaName', '', '')].isin(area_names_excluded))]

    # Apply positive slicing
    if len(area_names_included):
        dd = dd.loc[(dd[('areaName', '', '')].isin(area_names_included))]

    # Convert hierarchical columns to vector.
    dd = dd.droplevel(0, axis=1).droplevel(0, axis=1)

    # Change column names.
    dd.columns = [
        "Area name",
        "Area code",
        "date",
        *numeric_columns
    ]

    # Add "Area type" column.
    dd = dd.assign(**{"Area type": area_type})

    # Reorder columns and reset the index so that
    # it starts from zero and discard the missing
    # indices that have been filtered out.
    result = dd[[
        "Area name",
        "Area code",
        "Area type",
        "date",
        *numeric_columns
    ]].reset_index(drop=True)

    logging.info(
        f'>> Extracted data based on "{by}" - numeric columns: {numeric_columns}.'
    )
    return result


def generate_output_data(data: DataFrame, json_extras: ExtraJsonData,
                         output_cat: str, daily_records=True) -> InternalProcessor:
    """
    Applies the necessary rules to generate CSV and JSON data
    ready to be stored.

    Parameters
    ----------
    data: DataFrame
        Processed data.

    json_extras: ExtraJsonData
        Extra data to be included in the JSON file.

    output_cat: str
        Category name. Must be included in ``COLUMNS_BY_OUTPUT``.

    daily_records: bool
        Include daily records from ``json_extras``. [Default: True]

    Returns
    -------
    InternalProcessor
    """
    if output_cat not in COLUMNS_BY_OUTPUT:
        raise ValueError("Value of `output_cat` is not included in `COLUMNS_BY_OUTPUT`.")

    column_data = COLUMNS_BY_OUTPUT[output_cat]

    d = DataFrame()

    # Collect data and consolidate the results for
    # different criteria associated with each output file.
    for criteria in CRITERIA[output_cat]:
        df_temp = extract_data(data=data.copy(deep=True), **criteria)
        d = d.append(df_temp)

    # Sort the data (descending).
    d = d.sort_values(SORT_OUTPUT_BY, ascending=False)

    # Convert datetime object to string.
    # NOTE: Must be applied after the data has been sorted.
    d[DATE_COLUMN] = d[DATE_COLUMN] 

    # Create CSV output:
    # Column names converted as required.
    # NOTE: Due to the existence of NAN in the data, DataFrame
    # automatically stores numeric values as floating points.
    # Given that there is no floating point number in the outputs,
    # we can apply `float_format` and set it to "%d" to convert
    # values onto integer in CSV string output.
    csv = d.rename(
        columns=REPLACEMENT_COLUMNS['csv'][output_cat]
    ).to_csv(
        index=False,
        float_format="%d"
    )

    logging.info(">> CSV file generated.")

    # Converting column names as required for JSON output.
    d = d.rename(columns=REPLACEMENT_COLUMNS['json'][output_cat])

    json_meta = json_extras.copy()

    if not daily_records:
        json_meta.pop("dailyRecords")

    # Create JSON output.
    json = produce_json(
        data=d,
        json_extras=json_meta,
        numeric_columns=column_data['numeric_cols'],
        included_columns=column_data["included_cols"]
    )

    return InternalProcessor(csv=csv, json=json)


def process(data: DataFrame) -> GeneralProcessor:
    """
    Process the data and structure them in a 2D table.

    Parameters
    ----------
    data: DataFrame
        Original data.

    Returns
    -------
    GeneralProcessor
        Processed and structured data.
    """
    columns = ["value", "date", "areaCode", "areaType", "areaName", "category"]

    dt_final = DataFrame(columns=columns)

    # Because of the hierarchical nature of the original data, there is
    # no easy way to easily automate this process using a generic solution
    # without prolonging the execution time. The iterative method appears
    # to produce the optimal time.
    for area_type in CATEGORY_LABELS:
        dt_label = DataFrame(columns=columns)

        for area_code in data[area_type]:
            area_name = data[area_type][area_code]['name']['value']
            df_code = DataFrame(columns=columns)

            for category in VALUE_COLUMNS:
                if category not in data[area_type][area_code]:
                    continue

                df_value = json_normalize(data[area_type][area_code], [category], [])

                df_value["areaCode"] = area_code
                df_value["areaType"] = area_type
                df_value["areaName"] = area_name
                df_value["category"] = category

                df_code = df_code.append(df_value)

            dt_label = dt_label.append(df_code)

        dt_final = dt_final.append(dt_label)

    # Reset index to appear incrementally.
    dt_final = dt_final.reset_index()[columns]
    logging.info(">> Data was successfully processed.")

    # Convert date strings to timestamp objects (needed for sorting).
    dt_final[DATE_COLUMN] = to_datetime(dt_final[DATE_COLUMN])
    logging.info(">> Dates were successfully converted to datetime object.")

    # Create a hierarchy that allows aggregation as required
    # in output data.
    dt_final = dt_final.groupby(
        ["areaType", "category", "date", "areaName", "areaCode"]
    )
    logging.info(">> Data has been grouped.")

    # Given that the aggregation grouping produces rows with unique
    # value, the `sum()` function will produce the original value
    # or `NaN`.
    dt_final = dt_final.sum().unstack(
        ["areaType", "category"]
    )

    # Sort the data
    dt_final = dt_final.sort_values(
        ["date", "areaName"],
        ascending=False
    ).reset_index()
    logging.info(">> Data was successfully sorted by date and area name - descending.")

    metadata = Metadata(
        lastUpdatedAt=data['lastUpdatedAt'],
        disclaimer=data['disclaimer']
    )
    logging.info(">> Metadata extracted.")

    daily_records = DailyRecords(
        areaName="United Kingdom",
        totalLabConfirmedCases=None,
        dailyLabConfirmedCases=None
    )

    if (overview := data.get("overview")) is None:
        logging.warning(f'Missing data - Key: overview')

    elif (uk_cases := overview.get("K02000001")) is None:
        logging.warning(f'Missing data - Keys: overview > K02000001')
    else:
        for record_name, record_name_repl in DAILY_RECORD_LABELS.items():
            if (daily_record_item := uk_cases.get(record_name)) is not None:
                if (value := daily_record_item.get("value")) is not None:
                    daily_records[record_name_repl] = value
                    continue

            logging.warning(
                f'Missing data - Keys: overview > K02000001 > {record_name}'
            )

    logging.info(">> Daily records extracted.")

    extras = ExtraJsonData(
        metadata=metadata,
        dailyRecords=daily_records
    )

    return GeneralProcessor(data=dt_final, json_extras=extras)


def local_test(original_filepath: str) -> NoReturn:
    """
    Reads and loads the JSON data from ``original_filepath`` and
    runs the service, producing output files in the same directory.

    .. Note::
        The directory must contain subdirectories as ``downloads/json`` and
        ``downloads/csv``.

    Parameters
    ----------
    original_filepath: str
        Path to the original data.
    """
    with open(original_filepath, "r") as f:
        json_data = loads(f.read())

    processed = process(json_data)

    cases = generate_output_data(processed.data, processed.json_extras, CASES)
    deaths = generate_output_data(processed.data, processed.json_extras, DEATHS, False)

    with open("downloads/csv/coronavirus-cases.csv", "w") as file:
        print(cases.csv, file=file)

    with open("downloads/csv/coronavirus-deaths.csv", "w") as file:
        print(deaths.csv, file=file)

    with open("downloads/json/coronavirus-cases.json", "w") as file:
        print(cases.json, file=file)

    with open("downloads/json/coronavirus-deaths.json", "w") as file:
        print(deaths.json, file=file)


def main(newData: str,
         casesCsvOut: Out[str], casesJsonOut: Out[str],
         casesCsvOutLatest: Out[str], casesJsonOutLatest: Out[str],
         deathsCsvOut: Out[str], deathsJsonOut: Out[str],
         deathsCsvOutLatest: Out[str], deathsJsonOutLatest: Out[str],
         lastedJsonData: Out[str],
         context: Context) -> NoReturn:
    """
    Reads the data from the blob that has been updated, then runs it
    through the processors and produces the output by setting the
    the output values.

    See this for more -
    https://docs.microsoft.com/en-us/azure/azure-functions/functions-reference-python#outputs
    https://docs.microsoft.com/en-us/python/api/azure-functions/azure.functions.out?view=azure-python

    Parameters
    ----------
    newData: str
        JSON data for the new file that has been uploaded.

    casesCsvOut: Out[str]
        Dated CSV file setter for cases.

    casesJsonOut: Out[str]
        Dated JSON file setter for cases.

    casesCsvOutLatest: Out[str]
        CSV file setter for cases.

    casesJsonOutLatest: Out[str]
        JSON file setter for cases.

    deathsCsvOut: Out[str]
        Dated CSV file setter for deaths.

    deathsJsonOut: Out[str]
        Dated JSON file setter for deaths.

    deathsCsvOutLatest: Out[str]
        CSV file setter for deaths.

    deathsJsonOutLatest: Out[str]
        JSON file setter for deaths.

    lastedJsonData: Out[str]
        JSON data with new timestamp.

    context: Context
        Function triggering context.
    """

    logging.info(f"--- Blob update has triggered the function. Starting the process.")

    json_data = loads(newData)
    json_data["lastUpdatedAt"] = datetime.now().utcnow().isoformat() + 'Z'
    logging.info(f"> Loaded and parsed JSON data.")

    try:
        processed = process(json_data)
        logging.info(f"> Finished processing the data.")

        cases = generate_output_data(processed.data, processed.json_extras, CASES)
        logging.info(f'> Finished generating output data for "cases".')

        deaths = generate_output_data(processed.data, processed.json_extras, DEATHS, False)
        logging.info(f'> Finished generating output data for "deaths".')

    except Exception as e:
        logging.error(f'EXCEPTION: {e}')
        sys_exit(255)
        return

    casesCsvOut.set(cases.csv)
    logging.info(f'> Stored dated "cases" as CSV.')

    casesCsvOutLatest.set(cases.csv)
    logging.info(f'> Stored latest "cases" as CSV.')

    casesJsonOut.set(cases.json)
    logging.info(f'> Stored dated "cases" as JSON.')

    casesJsonOutLatest.set(cases.json)
    logging.info(f'> Stored latest "cases" as JSON.')

    deathsCsvOut.set(deaths.csv)
    logging.info(f'> Stored dated "deaths" as CSV.')

    deathsCsvOutLatest.set(deaths.csv)
    logging.info(f'> Stored latest "deaths" as CSV.')

    deathsJsonOut.set(deaths.json)
    logging.info(f'> Stored dated "deaths" as CSV.')

    deathsJsonOutLatest.set(deaths.json)
    logging.info(f'> Stored latest "deaths" as JSON.')

    lastedJsonData.set(dumps(json_data))
    logging.info(f'> Stored latest "data" as JSON.')

    logging.info(f"--- Process complete: exiting with code 0")
