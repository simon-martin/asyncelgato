import asyncio
import json
import logging
import socket
from time import time
from typing import Dict, List, Optional

from aiozeroconf import ServiceBrowser, ServiceInfo, Zeroconf
from aiohttp import ClientSession

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


class ElgatoLight(object):

    def __init__(self, address, port, name, server):
        """
        Address and port are used for contacting the light, name and server are currently only used
        for display purposes

        :param address: IP address of the light
        :param port: Port of the light
        :param name: Name of the light
        :param server: Server address
        """
        # Init info from discovery, or user controlled
        self.address = address
        self.port = port
        self.name = name
        self.server = server

        # Static info - populated on demand
        self._info: Optional[Dict[str, str]] = None

        # Status info - populated on demand
        self._status: Optional[Dict[str, int]] = None

    def __repr__(self) -> str:
        # Other info may be richer, but also may not be present
        return "Elgato Light {} @ {}:{}".format(self.name, self.address, self.port)

    @staticmethod
    def _temperature_to_value(temperature: int) -> int:
        """
        Take a color temp (in K) and convert it to the format the Elgato Light wants

        :param temperature: An temperature in K
        :return: An Elgato temperature value
        """
        return int(round(987007 * temperature ** -0.999, 0))

    @staticmethod
    def _value_to_temperature(value: int) -> int:
        """
        Take the int that the Elgato Light returns and convert it roughly back to color temp (in K)

        :param value: An Elgato temperature value
        :return: An temperature in K
        """
        return int(round(1000000 * value ** -1, 0))

    async def _elgato_rest(self, query: str, data: str = None) -> dict:
        """
        Call the rest API for this light

        :param query: API end point, e.g. lights, accessory-info
        :param data: optional data, if provided this will be put to the API
        :return: Response from the API in the form of a dictionary
        """
        session = ClientSession()
        try:
            if data:
                response = await session.put('http://{}:{}/elgato/{}'.format(self.address, self.port, query), data=data)
            else:
                response = await session.get('http://{}:{}/elgato/{}'.format(self.address, self.port, query))
            response_dict = await response.json()
        finally:
            await session.close()

        return response_dict

    async def info(self) -> Dict[str, str]:
        """
        Populates the class properties for model info
        """
        logger.debug("Populating info for {}".format(self))
        if self._info is None:
            self._info = await self._elgato_rest('accessory-info')
        return self._info.copy()

    async def status(self) -> Dict[str, int]:
        """
        Return the cached status, or retrieve from the light
        If other things are controlling the light this information could be wrong

        :return: A dictionary containing on, brightness and temperature values
        """
        logger.debug("Getting status for {}".format(self))

        # If we don't have the status already ask the light
        if self._status is None:
            status = await self._elgato_rest('lights')
            self._status = status['lights'][0]
            self._status['temperature'] = self._value_to_temperature(self._status['temperature'])

        return self._status.copy()

    async def set_status(self, on: int = None, brightness: int = None, temperature: int = None) -> Dict[str, int]:
        """
        Update the status of the light with the given values, and values not given will be unchanged

        :param on: 0 for off, 1 for on
        :param brightness: A value between 0 and 100
        :param temperature: A temperature in K between 2900 and 7000
        """
        logger.debug("Setting status for {} - {}, {}, {}".format(self, on, brightness, temperature))

        new_status: Dict[str, int] = {}
        if on is not None:
            if on in (0, 1):
                new_status["on"] = on
            else:
                logger.error("Invalid On - Must be 0 or 1")

        if brightness is not None:
            if 0 <= brightness <= 100:
                new_status["brightness"] = brightness
            else:
                logger.error("Invalid Brightness - Must be 0-100")

        if temperature is not None:
            if 2900 <= temperature <= 7000:
                new_status["temperature"] = self._temperature_to_value(temperature)
            else:
                logger.error("Invalid temperature - Must be 2900-7000")

        data_dict = {"numberOfLights": 1, "lights": [new_status]}
        data = json.dumps(data_dict)

        confirmed_status = await self._elgato_rest('lights', data)

        self._status = confirmed_status['lights'][0]
        self._status['temperature'] = self._value_to_temperature(self._status['temperature'])
        return self._status.copy()

    async def power_on(self) -> None:
        """
        Turns the light on
        """
        logger.debug("Turning on {}".format(self))
        await self.set_status(on=1)

    async def power_off(self) -> None:
        """
        Turns the light off
        """
        logger.debug("Turning off {}".format(self))
        await self.set_status(on=0)

    async def set_brightness(self, brightness: int) -> None:
        """
        Sets the light to a specific brightness (0-100) level

        :param brightness: A value between 0 and 100
        """
        logger.debug("Setting brightness {} on {}".format(brightness, self))
        await self.set_status(brightness=brightness)

    async def increment_brightness(self, increment: int) -> None:
        """
        Increases the light brightness by a set amount, if this goes below 0 or over 100, these will be used instead

        :param increment: A positive or negative value to change the brightness by
        """
        if self._status is None:
            await self.status()

        new_brightness = self._status['brightness'] + increment
        if new_brightness < 0:
            new_brightness = 0
        if new_brightness > 100:
            new_brightness = 100
        await self.set_brightness(new_brightness)

    async def set_temperature(self, temperature: int) -> None:
        """
        Sets the light to a specific color temperature (2900-7000k)

        :param temperature: A temperature in K between 2900 and 7000
        """
        await self.set_status(temperature=temperature)

    async def increment_temperature(self, increment: int) -> None:
        """
        Increases the light temperature by a set amount, if this goes below 2900 or over 7000, these will be used
        instead

        :param increment: A positive or negative value to change the brightness by
        """
        if self._status is None:
            await self.status()

        new_temperature = self._status['temperature'] + increment
        if new_temperature < 2900:
            new_temperature = 2900
        if new_temperature > 7000:
            new_temperature = 7000
        await self.set_temperature(new_temperature)


async def discover(light_count: int = None, timeout: int = 5) -> List[ElgatoLight]:
    """
    Look for Elgato Lights on the network, stop when either the target number of lights are found or we time out

    If you know the number of lights using this will speed up this operation without suffering from missing lights when
    combined with a decent timeout. On my network 5 seconds is plenty long enough for a timeout

    :param light_count: Number of lights we expect to find, if not given will only use timeout
    :param timeout: Number of seconds to look for
    :return: A list of lights found on the network
    """

    class MyListener(object):
        """
        Very basic listener will create light objects asynchronously
        """
        def __init__(self):
            self.future_services = []

        def add_service(self, zeroconf_: Zeroconf, type_: str, name: str) -> None:
            logger.debug("Adding service {}-{}".format(type_, name))
            # Not sure if there's much IO in this step, but we start it now just in case,
            # so it should be done by the time we need it
            future_service = asyncio.ensure_future(zeroconf_.get_service_info(type_, name))
            self.future_services.append(future_service)

        async def services(self) -> List[ServiceInfo]:
            return [await future for future in self.future_services]

    zeroconf_loop = Zeroconf(asyncio.get_event_loop())
    listener = MyListener()
    ServiceBrowser(zeroconf_loop, "_elg._tcp.local.", listener)

    lights = []
    try:
        start = time()
        # Listen until we find our target number of lights, or timeout
        while (light_count is None or len(listener.future_services) < light_count) and (time() - start) < timeout:
            await asyncio.sleep(0.1)

        for service_info in await listener.services():
            """
            For Elgato we discover ipv6 address but it's un-routable on my network, not sure whats going on
            Other devices discovered using the same method are routable
            If we want to do this we also need to change URL generation, need [ipv6] in aiohttp URL
            if service_info.address6:
                address = socket.inet_ntop(socket.AF_INET6, service_info.address6)
            else:
                address = socket.inet_ntop(socket.AF_INET, service_info.address)
            """

            address = socket.inet_ntop(socket.AF_INET, service_info.address)
            light = ElgatoLight(address=address,
                                port=int(service_info.port),
                                name=service_info.name,
                                server=service_info.server)
            lights.append(light)
    finally:
        await zeroconf_loop.close()

    return lights
