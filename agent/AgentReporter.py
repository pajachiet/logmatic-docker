import logging
import re

import copy

from agent.Calculator import Calculator

logger = logging.getLogger()


class AgentReporter:
    def __init__(self, client, logger, args):
        self.client = client
        self.logger = logger
        self.args = args
        self.calculator = Calculator()
        self.local_cache = {}
        self.attrs = {}
        self.daemon_name = self.client.info()["Name"]

        for attr in args.attrs:
            kv = attr.split("=")
            if len(kv) > 1:
                self.attrs[kv[0]] = kv[1]
            else:
                self.attrs[kv[0]] = ""

    def export_events(self):
        """Send events to Logmatic.io"""
        try:
            events = self.client.events(decode=True)
            for event in events:
                if event["Type"] == "container":
                    # get container meta
                    if event["id"] not in self.local_cache:
                        self._build_context(self.client.containers.get(event["id"]))

                    meta = self.local_cache[event["id"]]
                    meta["@marker"] = ["docker", "docker-events"]
                    meta[self.args.ns]["event"] = event["Action"]

                    # send it to Logmatic.io
                    self.logger.info("[Docker event] name:{} >> event:{} (image={})"
                                     .format(event["Actor"]["Attributes"]["name"],
                                             event["Action"],
                                             event["Actor"]["Attributes"]["image"]),
                                     extra=meta)

        except Exception:
            logger.exception("Unexpected error during the processing of events")

    def export_stats(self, container, detailed):
        """Send container stats to Logmatic.io"""
        try:
            meta = self._build_context(container)
            meta["@marker"] = ["docker", "docker-stats"]

            # call the API
            stats = container.stats(stream=False, decode=True)
            computed_stats = self.calculator.compute_human_stats(container, stats, detailed)
            meta[self.args.ns]["stats"] = computed_stats

            # format the event message
            message = ""
            if "error" not in computed_stats["cpu_stats"]:
                message += " cpu:{:.2f}%".format(computed_stats["cpu_stats"]["total_usage_pct"] * 100.0)
            if "error" not in computed_stats["memory_stats"]:
                message += " mem:{:.2f}%".format(computed_stats["memory_stats"]["usage_pct"] * 100.0)
            if "error" not in computed_stats["blkio_stats"]:
                message += " io:{:.2f}MB/s".format(computed_stats["blkio_stats"]["total_bps"] / 1000000.0)
            if "error" not in computed_stats["networks"]:
                message += " net:{:.2f}MB/s".format(
                    (computed_stats["networks"]["all"]["tx_bytes_ps"] + computed_stats["networks"]["all"][
                        "rx_bytes_ps"]) / 1000000.0)

            # send to Logmatic.io
            self.logger.info("[Docker stats] name:{} >> {} (host:{} image:{})"
                             .format(meta[self.args.ns]["name"],
                                     message,
                                     meta[self.args.ns]["hostname"],
                                     meta[self.args.ns]["image"]),
                             extra=meta)

        except Exception:
            logger.exception("Unexpected error during the processing of stats")

    def export_logs(self, container):
        """Send all container logs to Logmatic.io"""
        if container.attrs["Config"]["Image"].startswith("logmatic/logmatic-docker"):
            return
        try:
            line = ""
            meta = self._build_context(container)
            meta["@marker"] = ["docker", "docker-logs"]
            logs = container.logs(stream=True, follow=True, stdout=True, stderr=False, tail=0)
            for chunk in logs:
                # Append all char into a string until a \n
                if chunk is not '\n':
                    line = line + chunk
                else:
                    self.logger.info(line, extra=meta)
                    line = ""

        except Exception:
            logger.exception("Unexpected error during the processing of stats")

    def _build_context(self, container):
        """Internal method, build the container context"""
        try:

            # Checking the local cache
            if container.id in self.local_cache:
                meta = copy.deepcopy(self.local_cache[container.id])
                meta[self.args.ns]["status"] = container.status
                return meta

            # Concatenate all labels
            labels = {}
            if len(container.attrs["Config"]["Labels"]) > 0:
                labels["all"] = []
                for label in container.attrs["Config"]["Labels"]:
                    labels["all"].append(label)
                    if container.attrs["Config"]["Labels"][label] != "":
                        labels[label] = container.attrs["Config"]["Labels"][label]

            # Add all container/image information
            meta = {
                self.args.ns: {
                    "id": container.id,
                    "short_id": container.short_id,
                    "name": container.name,
                    "status": container.status,
                    "daemon_name": self.daemon_name,
                    "labels": labels,
                    "hostname": container.attrs["Config"]["Hostname"],
                    "image": container.attrs["Config"]["Image"],
                    "created": container.attrs["Created"],
                    "pid": container.attrs["State"]["Pid"]
                },
                "severity": "INFO"
            }

            # Add all attributes
            if len(self.attrs):
                meta["attr"] = self.attrs

            # Store in cache the object
            self.local_cache[container.id] = copy.deepcopy(meta)
            return meta

        except Exception:
            logger.exception("Unexpected error during the processing of stats")

    def filter(self, containers):
        """Expose only the containers and the images that match the rules set
            - skip_image: exclude all images matching the regex
            - match_image: keep all images matching the regex
            - skip_container: exclude all containers matching the regex
            - match_container: keep all containers matching the regex
        """
        filtered = []

        # Continue if no filter has been set
        if not (self.args.skip_name or self.args.skip_image or self.args.match_name or self.args.match_image):
            logger.debug("Filter // Keeping all containers")
            return containers

        for c in containers:
            # skip container by image name
            if self.args.skip_image and re.search(self.args.skip_image, c.attrs["Config"]["Image"]):
                continue
            # skip container by container name
            if self.args.skip_name and re.search(self.args.skip_name, c.name):
                continue
            # match container by image name
            if self.args.match_image and not re.search(self.args.match_image, c.attrs["Config"]["Image"]):
                continue
            # match container by name name
            if self.args.match_name and not re.search(self.args.match_name, c.name):
                continue

            filtered.append(c)

        logger.debug("Filter // Keeping containers: {}".format(filtered))
        return filtered
