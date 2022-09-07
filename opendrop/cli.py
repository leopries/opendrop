"""
OpenDrop: an open source AirDrop implementation
Copyright (C) 2018  Milan Stute
Copyright (C) 2018  Alexander Heinrich

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.
"""

import argparse
from concurrent.futures import thread
import json
import logging
import multiprocessing
import os
import sys
from tarfile import LENGTH_LINK
import threading
import time

from sympy import arg

from .client import AirDropBrowser, AirDropClient
from .config import AirDropConfig, AirDropReceiverFlags
from .server import AirDropServer

logger = logging.getLogger(__name__)


def main():
    AirDropCli(sys.argv[1:])


class AirDropCli:

    requested_receivers = []
    currently_requesting = False

    number_accepted_requests = 0

    # duration in seconds until a request times out and new request to new devices will be send
    timeout_duration = 2

    def __init__(self, args):
        parser = argparse.ArgumentParser()
        parser.add_argument("action", choices=["receive", "find", "send"])
        parser.add_argument("-f", "--file", help="The URL that will be sent to everyone nearby.")
        parser.add_argument(
            "-r",
            "--receiver",
            help="Peer to send file to (can be index, ID, or hostname)",
        )
        parser.add_argument(
            "-e", "--email", nargs="*", help="User's email addresses (currently unused)"
        )
        parser.add_argument(
            "-p", "--phone", nargs="*", help="User's phone numbers (currently unused)"
        )
        parser.add_argument(
            "-n", "--name", help="Computer name (displayed in sharing pane)"
        )
        parser.add_argument(
            "-m", "--model", help="Computer model (displayed in sharing pane)"
        )
        parser.add_argument(
            "-d", "--debug", help="Enable debug mode", action="store_true"
        )
        parser.add_argument(
            "-i", "--interface", help="Which AWDL interface to use", default="awdl0"
        )
        args = parser.parse_args(args)

        if args.debug:
            logging.basicConfig(
                level=logging.DEBUG,
                format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
            )
        else:
            logging.basicConfig(level=logging.INFO, format="%(message)s")

        # TODO put emails and phone in canonical form (lower case, no '+' sign, etc.)

        self.config = AirDropConfig(
            email=args.email,
            phone=args.phone,
            computer_name=args.name,
            computer_model=args.model,
            debug=args.debug,
            interface=args.interface,
        )
        self.server = None
        self.client = None
        self.browser = None
        self.sending_started = False
        self.discover = []
        self.lock = threading.Lock()
        try:
            # hardcoded to execute spam call
            self.file = args.file
            self.is_url = True
            self.config.computer_name = "BarMinga."
            self.config.computer_model = "BarMinga."

            # start of request spamming wrapper that can be stopped with a keyboard interrupt
            self.spam_wrapper()
            
        except KeyboardInterrupt:
            if self.browser is not None:
                self.browser.stop()
            if self.server is not None:
                self.server.stop()
    
    def spam_wrapper(self):
        try:
            logger.info("Looking for receivers. Press Ctrl+C to stop ...")
            self.find()
            threading.Event().wait()
        except KeyboardInterrupt:
            pass
        finally:
            self.browser.stop()
            logger.info(str(self.number_accepted_requests) + "/" + str(len(self.requested_receivers)) + " devices accepted.")
            logger.debug(f"Save discovery results to {self.config.discovery_report}")
            with open(self.config.discovery_report, "w") as f:
                json.dump(self.discover, f)

    # start a new airdrop server and search for devices
    def find(self):
        logger.info("(Re-)Started AirDrop server and searching for results...")
        self.browser = AirDropBrowser(self.config)
        self.browser.start(callback_add=self._found_receiver)

    # Send discovery message in new thread if following requirements are met for the found device
    # 1. the receiver has not been requested already
    # 2. no other discovery is running currently
    def _found_receiver(self, info):
        if self._device_already_requested(info):
            logger.info("Skipped discovering device because device has already been requested.")
            return
        if self.currently_requesting:
            logger.info("Skipped discovering device because another request is currently running.")
            return

        self.currently_requesting = True
        self.requested_receivers.append(info.key)

        # this thread will stop and restart the airdrop server after sending a file
        thread = threading.Thread(target=self._send_discover, args=(info,))
        thread.start()
    
    # Checks if a device has already been requested based on the ID
    # TODO: check validity expiration time of ID (or other limitations)
    def _device_already_requested(self, info):
        if info.key in self.requested_receivers:
            return True
        return False

    def _send_discover(self, info):
        try:
            address = info.parsed_addresses()[0]  # there should only be one address
        except IndexError:
            logger.warning(f"Ignoring receiver with missing address {info}")
            return
        identifier = info.name.split(".")[0]
        hostname = info.server
        port = int(info.port)
        logger.debug(f"AirDrop service found: {hostname}, {address}:{port}, ID {id}")
        client = AirDropClient(self.config, (address, int(port)), timeout=self.timeout_duration)
        try:
            flags = int(info.properties[b"flags"])
        except KeyError:
            # TODO in some cases, `flags` are not set in service info; for now we'll try anyway
            flags = AirDropReceiverFlags.SUPPORTS_DISCOVER_MAYBE

        if flags & AirDropReceiverFlags.SUPPORTS_DISCOVER_MAYBE:
            try:
                receiver_name = client.send_discover()
            except TimeoutError:
                receiver_name = None
        else:
            receiver_name = None
        discoverable = receiver_name is not None

        index = len(self.discover)
        node_info = {
            "name": receiver_name,
            "address": address,
            "port": port,
            "id": identifier,
            "flags": flags,
            "discoverable": discoverable,
        }

        self.lock.acquire()
        self.discover.append(node_info)
        if discoverable:
            logger.info(f"Found  index {index}  ID {identifier}  name {receiver_name}")
        else:
            logger.debug(f"Receiver ID {identifier} is not discoverable")
        self.lock.release()

        # send after successfully discovered the device
        self.send(receiver = node_info)
        # Reset flag if a sending request is currently running
        self.currently_requesting = False

        # Stop the server
        self.browser.stop()

        # Restart the server by searching for new devices
        self.find()

    def send(self, receiver):
        if receiver is None:
            return
        self.client = AirDropClient(self.config, (receiver["address"], receiver["port"]), timeout=self.timeout_duration)
        logger.info("Asking receiver to accept ...")

        # Ensure timeout
        try:
            ask_res = self.client.send_ask(self.file, is_url=self.is_url)
        except TimeoutError:
            logger.info("Connection request timed out. No new requests will be send to this device.")
            return
        
        if not ask_res:
            logger.warning("Receiver declined")
            return
        logger.info("Receiver accepted")
        logger.info("Uploading file ...")
        # if the file is an url (and not an actual file), the file upload request is skipped
        if (not self.is_url) and (not self.client.send_upload(self.file, is_url=self.is_url)):
            logger.warning("Uploading has failed")
            return
        self.number_accepted_requests += 1
        logger.info("Uploading has been successful")
