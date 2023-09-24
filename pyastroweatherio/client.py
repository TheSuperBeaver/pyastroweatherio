"""Define a client to interact with 7Timer."""
import asyncio
import json
import logging
from datetime import datetime, timedelta
from typing import Optional
from decimal import Decimal
from aiohttp import ClientSession, ClientTimeout
from aiohttp.client_exceptions import ClientError

from pyastroweatherio.const import (
    BASE_URL_SEVENTIMER,
    BASE_URL_MET,
    DEFAULT_TIMEOUT,
    DEFAULT_CACHE_TIMEOUT,
    DEFAULT_ELEVATION,
    DEFAULT_TIMEZONE,
    DEFAULT_CONDITION_CLOUDCOVER_WEIGHT,
    DEFAULT_CONDITION_SEEING_WEIGHT,
    DEFAULT_CONDITION_TRANSPARENCY_WEIGHT,
    HOME_LATITUDE,
    HOME_LONGITUDE,
    STIMER_OUTPUT,
    # FORECAST_TYPE_DAILY,
    FORECAST_TYPE_HOURLY,
    MAGNUS_COEFFICIENT_A,
    MAGNUS_COEFFICIENT_B,
)
from pyastroweatherio.dataclasses import (
    ForecastData,
    LocationData,
    NightlyConditionsData,
)
from pyastroweatherio.errors import RequestError
from pyastroweatherio.helper_functions import ConversionFunctions, AstronomicalRoutines

_LOGGER = logging.getLogger(__name__)


class AstroWeather:
    """AstroWeather Communication Client."""

    def __init__(
        self,
        session: Optional[ClientSession] = None,
        latitude=HOME_LATITUDE,
        longitude=HOME_LONGITUDE,
        elevation=DEFAULT_ELEVATION,
        timezone_info=DEFAULT_TIMEZONE,
        cloudcover_weight=DEFAULT_CONDITION_CLOUDCOVER_WEIGHT,
        seeing_weight=DEFAULT_CONDITION_SEEING_WEIGHT,
        transparency_weight=DEFAULT_CONDITION_TRANSPARENCY_WEIGHT,
        metno_enabled=True,
    ):
        self._session: ClientSession = session
        self._latitude = latitude
        self._longitude = longitude
        self._elevation = elevation
        self._timezone_info = timezone_info
        self._weather_data_seventimer = []
        self._weather_data_seventimer_init = ""
        self._weather_data_metno = []
        self._weather_data_metno_init = ""
        self._weather_data_seventimer_timestamp = datetime.now() - timedelta(seconds=(DEFAULT_CACHE_TIMEOUT + 1))
        self._weather_data_metno_timestamp = datetime.now() - timedelta(seconds=(DEFAULT_CACHE_TIMEOUT + 1))
        self._cloudcover_weight = cloudcover_weight
        self._seeing_weight = seeing_weight
        self._transparency_weight = transparency_weight
        self._metno_enabled = metno_enabled
        self.req = session

    # Public functions
    async def get_location_data(
        self,
    ) -> None:
        """Returns station Weather Forecast."""
        return await self._get_location_data()

    # async def get_forecast(self, forecast_type=FORECAST_TYPE_DAILY, hours_to_show=24) -> None:
    #     """Returns station Weather Forecast."""
    #     _LOGGER.debug("get_forecast called")
    #     return await self._forecast_data(forecast_type, hours_to_show)

    # async def get_daily_forecast(self) -> None:
    #     """Returns daily Weather Forecast."""
    #     return await self._forecast_data(FORECAST_TYPE_DAILY, 72)

    async def get_hourly_forecast(self) -> None:
        """Returns hourly Weather Forecast."""
        return await self._forecast_data(FORECAST_TYPE_HOURLY, 72)

    async def get_deepsky_forecast(self) -> None:
        """Returns Deep Sky Forecast."""
        return await self._deepsky_forecast()

    # Private functions
    async def _get_location_data(self) -> None:
        """Return Forecast data"""

        cnv = ConversionFunctions()
        items = []

        await self.retrieve_data_seventimer()
        if self._metno_enabled:
            await self.retrieve_data_metno()
        now = datetime.utcnow()

        # Anchor timestamp
        init_ts = await cnv.anchor_timestamp(self._weather_data_seventimer_init)

        # Met.no
        metno_index = -1
        forecast_skipped = 0
        for row in self._weather_data_seventimer:
            # 7Timer: Skip over past forecasts
            forecast_time = init_ts + timedelta(hours=row["timepoint"])
            if now > forecast_time:
                forecast_skipped += 1
                continue

            _LOGGER.debug("7Timer forecast time: %s", str(forecast_time))

            # Met.no: Search for 7Timer forecast time
            if self._metno_enabled:
                for datapoint in self._weather_data_metno:
                    metno_index += 1
                    if forecast_time == datetime.strptime(datapoint.get("time"), "%Y-%m-%dT%H:%M:%SZ"):
                        break
                _LOGGER.debug("Met.no start index: %s", str(metno_index))

            # Astro Routines
            astro_routines = AstronomicalRoutines(
                self._latitude,
                self._longitude,
                self._elevation,
                self._timezone_info,
                now,
            )

            item = {
                "init": init_ts,
                "timepoint": row["timepoint"],
                "timestamp": forecast_time,
                "forecast_length": (len(self._weather_data_seventimer) - forecast_skipped) * 3,
                "latitude": self._latitude,
                "longitude": self._longitude,
                "elevation": self._elevation,
                "cloudcover": row["cloudcover"],
                "seeing": row["seeing"],
                "transparency": row["transparency"],
                # "condition_percentage": await self.calc_condition_percentage(
                #     row["cloudcover"], row["seeing"], row["transparency"]
                # ),
                "lifted_index": row["lifted_index"],
                "rh2m": row["rh2m"],
                "wind10m": row["wind10m"],
                "temp2m": row["temp2m"],
                "dewpoint2m": await self.calc_dewpoint2m(row["rh2m"], row["temp2m"]),
                "prec_type": row["prec_type"],
                "sun_next_rising": await astro_routines.sun_next_rising(),
                "sun_next_rising_nautical": await astro_routines.sun_next_rising_nautical(),
                "sun_next_rising_astro": await astro_routines.sun_next_rising_astro(),
                "sun_next_setting": await astro_routines.sun_next_setting(),
                "sun_next_setting_nautical": await astro_routines.sun_next_setting_nautical(),
                "sun_next_setting_astro": await astro_routines.sun_next_setting_astro(),
                "sun_altitude": await astro_routines.sun_altitude(),
                "sun_azimuth": await astro_routines.sun_azimuth(),
                "moon_next_rising": await astro_routines.moon_next_rising(),
                "moon_next_setting": await astro_routines.moon_next_setting(),
                "moon_phase": await astro_routines.moon_phase(),
                "moon_altitude": await astro_routines.moon_altitude(),
                "moon_azimuth": await astro_routines.moon_azimuth(),
                "weather": row.get("weather", ""),
                "deepsky_forecast": await self._deepsky_forecast(),
            }
            # Met.no
            if (
                self._metno_enabled
                and datetime.strptime(
                    self._weather_data_metno[metno_index].get("time"),
                    "%Y-%m-%dT%H:%M:%SZ",
                )
                == forecast_time
            ):
                # _LOGGER.debug("Met.no Cloud Area Fraction timestamp match: %s", str(forecast_time))
                datails = self._weather_data_metno[metno_index].get("data", {}).get("instant", {}).get("details", {})
                # Overwrite cloudcover
                item["cloudcover"] = int(datails.get("cloud_area_fraction", -1) / 12.5 + 1)

                item["cloud_area_fraction"] = datails.get("cloud_area_fraction", -1)
                item["cloud_area_fraction_high"] = datails.get("cloud_area_fraction_high", -1)
                item["cloud_area_fraction_low"] = datails.get("cloud_area_fraction_low", -1)
                item["cloud_area_fraction_medium"] = datails.get("cloud_area_fraction_medium", -1)

                item["condition_percentage"] = await self.calc_condition_percentage(
                    item["cloud_area_fraction"] / 12.5 + 1,
                    row["seeing"],
                    row["transparency"],
                )
            else:
                # _LOGGER.debug("Met.no no Cloud Area Fraction for: %s", str(forecast_time))
                item["cloud_area_fraction"] = None
                item["cloud_area_fraction_high"] = None
                item["cloud_area_fraction_low"] = None
                item["cloud_area_fraction_medium"] = None

                item["condition_percentage"] = await self.calc_condition_percentage(
                    row["cloudcover"], row["seeing"], row["transparency"]
                )

            items.append(LocationData(item))
            break

        return items

    async def _forecast_data(self, forecast_type, hours_to_show) -> None:
        """Return Forecast data for the Station."""

        cnv = ConversionFunctions()
        items = []

        await self.retrieve_data_seventimer()
        if self._metno_enabled:
            await self.retrieve_data_metno()
        now = datetime.utcnow()

        # Create items
        cnt = 0

        # Anchor timestamp
        init_ts = await cnv.anchor_timestamp(self._weather_data_seventimer_init)

        # Astro Routines
        astro_routines = AstronomicalRoutines(
            self._latitude, self._longitude, self._elevation, self._timezone_info, now
        )
        utc_to_local_diff = astro_routines.utc_to_local_diff()
        _LOGGER.debug("UTC to local diff: %s", str(utc_to_local_diff))
        _LOGGER.debug("Forecast length: %s", str(len(self._weather_data_seventimer)))

        # Met.no
        metno_index = -1
        for row in self._weather_data_seventimer:
            # 7Timer: Skip over past forecasts
            forecast_time = init_ts + timedelta(hours=row["timepoint"])
            if now > forecast_time:
                continue

            # Met.no: Search for 7Timer forecast time
            if self._metno_enabled:
                if metno_index == -1:
                    for datapoint in self._weather_data_metno:
                        metno_index += 1
                        if forecast_time == datetime.strptime(datapoint.get("time"), "%Y-%m-%dT%H:%M:%SZ"):
                            break
                    _LOGGER.debug("Met.no start index: %s", str(metno_index))

            # Hour of day needs to be in local time
            hour_of_day = (forecast_time.hour + utc_to_local_diff) % 24

            cloudcover = row["cloudcover"]
            seeing = row["seeing"]
            transparency = row["transparency"]

            item = {
                "init": init_ts,
                "timepoint": row["timepoint"],
                "timestamp": forecast_time,
                # "timestamp": astro_routines.utc_to_local(forecast_time),
                "hour": hour_of_day,
                "cloudcover": cloudcover,
                "seeing": seeing,
                "transparency": transparency,
                "lifted_index": row["lifted_index"],
                "rh2m": row["rh2m"],
                "wind10m": row["wind10m"],
                "temp2m": row["temp2m"],
                "dewpoint2m": await self.calc_dewpoint2m(row["rh2m"], row["temp2m"]),
                "prec_type": row["prec_type"],
                "weather": row.get("weather", ""),
            }
            # Met.no
            if (
                self._metno_enabled
                and datetime.strptime(
                    self._weather_data_metno[metno_index + cnt].get("time"),
                    "%Y-%m-%dT%H:%M:%SZ",
                )
                == forecast_time
            ):
                # _LOGGER.debug("Met.no Cloud Area Fraction timestamp match: %s", str(forecast_time))
                # Continue hourly and overwrite cloudcover while leaving the rest from 7timer
                for i in range(0, 3):
                    datails = (
                        self._weather_data_metno[metno_index + cnt + i].get("data", {}).get("instant", {}).get("details", {})
                    )
                    # Overwrite cloudcover
                    item["cloudcover"] = int(datails.get("cloud_area_fraction", -1) / 12.5 + 1)

                    item["cloud_area_fraction"] = datails.get("cloud_area_fraction", -1)
                    item["cloud_area_fraction_high"] = datails.get("cloud_area_fraction_high", -1)
                    item["cloud_area_fraction_low"] = datails.get("cloud_area_fraction_low", -1)
                    item["cloud_area_fraction_medium"] = datails.get("cloud_area_fraction_medium", -1)

                    item["condition_percentage"] = await self.calc_condition_percentage(
                        item["cloud_area_fraction"] / 12.5 + 1,
                        row["seeing"],
                        row["transparency"],
                    )
                    items.append(ForecastData(item))
                
                    item["timepoint"] = item["timepoint"] + 1
                    item["timestamp"] = item["timestamp"] + timedelta(hours=1)
                    item["hour"] = item["hour"] + 1
            else:
                # _LOGGER.debug("Met.no no Cloud Area Fraction for: %s", str(forecast_time))
                item["cloud_area_fraction"] = None
                item["cloud_area_fraction_high"] = None
                item["cloud_area_fraction_low"] = None
                item["cloud_area_fraction_medium"] = None

                item["condition_percentage"] = await self.calc_condition_percentage(
                    row["cloudcover"], row["seeing"], row["transparency"]
                )
                items.append(ForecastData(item))

            # Limit number of Hours
            cnt += 3
            if cnt >= hours_to_show:
                break

        return items

    async def _deepsky_forecast(self):
        """Return Deepsky Forecast data"""

        cnv = ConversionFunctions()
        items = []

        await self.retrieve_data_seventimer()
        if self._metno_enabled:
            await self.retrieve_data_metno()
        now = datetime.utcnow()

        # Create items
        cnt = 0

        # Anchor timestamp
        init_ts = await cnv.anchor_timestamp(self._weather_data_seventimer_init)

        # Astro Routines
        astro_routines = AstronomicalRoutines(
            self._latitude, self._longitude, self._elevation, self._timezone_info, now
        )
        utc_to_local_diff = astro_routines.utc_to_local_diff()

        # Create forecast
        forecast_dayname = ""
        start_forecast_hour = 0
        start_weather = ""
        interval_points = []

        # Met.no
        metno_index = -1
        for row in self._weather_data_seventimer:
            # Skip over past forecasts
            forecast_time = init_ts + timedelta(hours=row["timepoint"])
            if now > forecast_time:
                continue

            # Met.no
            if self._metno_enabled:
                if metno_index == -1:
                    for datapoint in self._weather_data_metno:
                        metno_index += 1
                        if forecast_time == datetime.strptime(datapoint.get("time"), "%Y-%m-%dT%H:%M:%SZ"):
                            break

            # Hour of day needs to be in local time
            hour_of_day = (forecast_time.hour + utc_to_local_diff) % 24

            # Skip daytime, we're only interested in the forecasts in
            # between 9pm to 3am.
            # Possible timestamps within the data:
            # 15 18 (21 00 03) 06 09 12
            # 16 (19 22 01) 04 07 10 13
            # 17 (20 23 02) 05 08 11 14
            # Relevant ones in brackets
            if hour_of_day < 19 and hour_of_day > 3:
                start_forecast_hour = 0
                start_weather = ""
                interval_points = []
                cnt += 3
                continue

            cloudcover = row["cloudcover"]
            seeing = row["seeing"]
            transparency = row["transparency"]
            cloud_area_fraction = 0
            # Met.no
            if (
                self._metno_enabled
                and datetime.strptime(
                    self._weather_data_metno[metno_index + cnt].get("time"),
                    "%Y-%m-%dT%H:%M:%SZ",
                )
                == forecast_time
            ):
                # Met.no
                # _LOGGER.debug("Cloud Area Fraction timestamp match: %s", str(forecast_time))
                datails = (
                    self._weather_data_metno[metno_index + cnt].get("data", {}).get("instant", {}).get("details", {})
                )
                cloud_area_fraction = datails.get("cloud_area_fraction") / 12.5 + 1
            # else:
            #     _LOGGER.debug("No Cloud Area Fraction for: %s", str(forecast_time))

            if len(interval_points) == 0:
                forecast_dayname = forecast_time.strftime("%A")
                start_forecast_hour = hour_of_day
                start_weather = row.get("weather", "")

            # Calculate Condition
            if self._metno_enabled and cloud_area_fraction > 0:
                interval_points.append(await self.calc_condition_percentage(cloud_area_fraction, seeing, transparency))
            else:
                interval_points.append(await self.calc_condition_percentage(cloudcover, seeing, transparency))

            if len(interval_points) == 3:
                item = {
                    "init": init_ts,
                    "dayname": forecast_dayname,
                    "hour": start_forecast_hour,
                    "nightly_conditions": interval_points,
                    "weather": start_weather,
                }
                items.append(NightlyConditionsData(item))
                _LOGGER.debug(
                    "Nightly conditions day: %s, start hour: %s, condition percentages: %s",
                    str(forecast_dayname),
                    str(start_forecast_hour),
                    str(interval_points),
                )
            cnt += 3
            if len(items) == 2:
                break

        return items

    async def calc_condition_percentage(self, cloudcover, seeing, transparency):
        """Return condition based on cloud cover, seeing and transparency"""
        # Possible Values:
        #   Clouds: 1-9
        #   Seeing: 1-8
        #   Transparency: 1-8
        condition = int(
            100
            - (
                self._cloudcover_weight * cloudcover
                + self._seeing_weight * seeing
                + self._transparency_weight * transparency
                - self._cloudcover_weight
                - self._seeing_weight
                - self._transparency_weight
            )
            * 100
            / (
                self._cloudcover_weight * 9
                + self._seeing_weight * 8
                + self._transparency_weight * 8
                - self._cloudcover_weight
                - self._seeing_weight
                - self._transparency_weight
            )
        )
        # _LOGGER.debug(
        #     "Calc condition cloudcover: %d(%d), seeing %d(%d), transparency: %d(%d), condition %d",
        #     cloudcover,
        #     self._cloudcover_weight,
        #     seeing,
        #     self._seeing_weight,
        #     transparency,
        #     self._transparency_weight,
        #     condition,
        # )
        return condition

    async def calc_dewpoint2m(self, rh2m, temp2m):
        """Calculate 2m Dew Point."""
        # α(T,RH) = ln(RH/100) + aT/(b+T)
        # Ts = (b × α(T,RH)) / (a - α(T,RH))
        alpha = float(Decimal(str(rh2m / 100)).ln()) + MAGNUS_COEFFICIENT_A * temp2m / (MAGNUS_COEFFICIENT_B + temp2m)
        dewpoint = (MAGNUS_COEFFICIENT_B * alpha) / (MAGNUS_COEFFICIENT_A - alpha)

        return dewpoint

    async def retrieve_data_seventimer(self):
        """Retrieves current data from 7timer"""

        if ((datetime.now() - self._weather_data_seventimer_timestamp).total_seconds()) > DEFAULT_CACHE_TIMEOUT:
            self._weather_data_seventimer_timestamp = datetime.now()
            _LOGGER.debug("Updating data from 7Timer")

            # Testing
            # json_data_astro = {"init": "2022060906"}
            # with open("astro.json") as json_file:
            #     astro_dataseries = json.load(json_file).get("dataseries", {})
            # with open("civil.json") as json_file:
            #     civil_dataseries = json.load(json_file).get("dataseries", {})
            # -Testing
            json_data_astro = await self.async_request_seventimer("astro", "get")
            json_data_civil = await self.async_request_seventimer("civil", "get")

            astro_dataseries = json_data_astro.get("dataseries", {})
            civil_dataseries = json_data_civil.get("dataseries", {})
            # /Testing

            for astro, civil in zip(astro_dataseries, civil_dataseries):
                if astro["timepoint"] == civil["timepoint"]:
                    astro["weather"] = civil["weather"]
                    astro["rh2m"] = int(civil["rh2m"].replace("%", ""))

            self._weather_data_seventimer = astro_dataseries
            self._weather_data_seventimer_init = json_data_astro.get("init")
        else:
            _LOGGER.debug("Using cached data for 7Timer")

    async def async_request_seventimer(self, product="astro", method="get") -> dict:
        """Make a request against the 7timer API."""

        use_running_session = self._session and not self._session.closed

        if use_running_session:
            session = self._session
        else:
            session = ClientSession(
                timeout=ClientTimeout(total=DEFAULT_TIMEOUT),
            )

        # BASE_URL_SEVENTIMER = "https://www.7timer.info/bin/api.pl?lon=XX.XX&lat=YY.YY&product=astro&output=json"
        # STIMER_OUTPUT = "json"
        url = (
            str(f"{BASE_URL_SEVENTIMER}")
            + "?lon="
            + str("%.1f" % round(self._longitude, 2))
            + "&lat="
            + str("%.1f" % round(self._latitude, 2))
            + "&product="
            + str(product)
            + "&output="
            + STIMER_OUTPUT
        )
        try:
            _LOGGER.debug(f"Query url: {url}")
            async with session.request(method, url) as resp:
                resp.raise_for_status()
                plain = str(await resp.text()).replace("\n", " ")
                data = json.loads(plain)

                # Testing
                # json_string = json.dumps(data)
                # with open(product + ".json", "w") as outfile:
                #     outfile.write(json_string)
                # /Testing

                return data
        except asyncio.TimeoutError as tex:
            raise RequestError(f"Request to endpoint timed out: {tex}") from None
        except ClientError as err:
            raise RequestError(f"Error requesting data: {err}") from None

        finally:
            if not use_running_session:
                await session.close()

    async def retrieve_data_metno(self):
        """Retrieves current data from met"""

        if ((datetime.now() - self._weather_data_metno_timestamp).total_seconds()) > DEFAULT_CACHE_TIMEOUT:
            self._weather_data_metno_timestamp = datetime.now()
            _LOGGER.debug("Updating data from Met.no")

            # Testing
            # json_data_astro = {"init": "2022060906"}
            # with open("astro.json") as json_file:
            #     astro_dataseries = json.load(json_file).get("dataseries", {})
            # with open("civil.json") as json_file:
            #     civil_dataseries = json.load(json_file).get("dataseries", {})
            # -Testing
            json_data_metno = await self.async_request_met("met", "get")

            dataseries = json_data_metno.get("properties", {}).get("timeseries", [])
            # /Testing

            self._weather_data_metno = dataseries
            self._weather_data_metno_init = dataseries[0].get("time", None)
        else:
            _LOGGER.debug("Using cached data for Met.no")

    async def async_request_met(self, product="met", method="get") -> dict:
        """Make a request against the 7timer API."""

        use_running_session = self._session and not self._session.closed

        if use_running_session:
            session = self._session
        else:
            session = ClientSession(
                timeout=ClientTimeout(total=DEFAULT_TIMEOUT),
            )

        # BASE_URL_MET = "https://api.met.no/weatherapi/locationforecast/2.0/complete?altitude=XX&lat=XX.XX&lon=XX.XX"
        url = (
            str(f"{BASE_URL_MET}")
            + "?lon="
            + str("%.1f" % round(self._longitude, 2))
            + "&lat="
            + str("%.1f" % round(self._latitude, 2))
            + "&altitude="
            + str(self._elevation)
        )
        try:
            _LOGGER.debug(f"Query url: {url}")
            async with session.request(method, url) as resp:
                resp.raise_for_status()
                # plain = str(await resp.text()).replace("\n", " ")
                # data = json.loads(plain)
                data = await resp.json()

                # Testing
                # json_string = json.dumps(data)
                # with open(product + ".json", "w") as outfile:
                #     outfile.write(json_string)
                # /Testing

                return data
        except asyncio.TimeoutError as tex:
            raise RequestError(f"Request to endpoint timed out: {tex}") from None
        except ClientError as err:
            raise RequestError(f"Error requesting data: {err}") from None

        finally:
            if not use_running_session:
                await session.close()
