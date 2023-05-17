"""
Create an image wall of the most-used images
"""
import colorsys
import random
import shutil
import math
import shlex
import subprocess

from PIL import Image, ImageFile, ImageOps, ImageDraw, UnidentifiedImageError
from sklearn.cluster import KMeans
from operator import mul
from functools import reduce
from random import random

import common.config_manager as config

from common.lib.helpers import UserInput, convert_to_int
from backend.abstract.processor import BasicProcessor
from common.lib.exceptions import ProcessorInterruptedException

__author__ = "Stijn Peeters"
__credits__ = ["Stijn Peeters"]
__maintainer__ = "Stijn Peeters"
__email__ = "4cat@oilab.eu"


class VideoWallGenerator(BasicProcessor):
    """
    Image wall generator

    Create an image wall from the top images in the dataset
    """
    type = "video-wall"  # job type ID
    category = "Visual"  # category
    title = "Video wall"  # title displayed in UI
    description = "Put all videos in a single combined video, side by side. Videos can be sorted and resized."
    extension = "mp4"  # extension of result file, used internally and in UI

    options = {
        "amount": {
            "type": UserInput.OPTION_TEXT,
            "help": "No. of videos (max 100)",
            "default": 25,
            "min": 0,
            "max": 100,
            "tooltip": "'0' uses as many videos as available in the archive (up to 100)"
        },
        "tile-size": {
            "type": UserInput.OPTION_CHOICE,
            "options": {
                "square": "Square",
                "average": "Average video in set",
                "fit-height": "Fit height"
            },
            "default": "square",
            "help": "Video tile size",
            "tooltip": "'Fit height' retains width/height ratios but makes videos have the same height"
        },
        "sort-mode": {
            "type": UserInput.OPTION_CHOICE,
            "help": "Sort videos by",
            "options": {
                "": "Do not sort",
                "random": "Random",
                "shortest": "Length (shortest first)",
                "longest": "Length (longest first)"
            },
            "default": "shortest"
        },
        "audio": {
            "type": UserInput.OPTION_CHOICE,
            "help": "Audio handling",
            "options": {
                "longest": "Use audio from longest video in video wall",
                "none": "Remove audio"
            },
            "default": "longest"
        }
    }

    # videos will be arranged and resized to fit these image wall dimensions
    # note that video aspect ratio may not allow for a precise fit
    TARGET_WIDTH = 2560
    TARGET_HEIGHT = 1440

    @classmethod
    def is_compatible_with(cls, module=None):
        """
        Determine compatibility

        :param str module:  Module ID to determine compatibility with
        :return bool:
        """
        if module.is_dataset() and module.num_rows > 50:
            # this processor doesn't work well with large datasets
            return False

        # also need ffprobe to determine video lengths
        # is usually installed in same place as ffmpeg
        ffmpeg_path = shutil.which(config.get("video_downloader.ffmpeg-path"))
        ffprobe_path = shutil.which("ffprobe".join(ffmpeg_path.rsplit("ffmpeg", 1)))

        return module.type.startswith("video-downloader") and \
               ffmpeg_path and \
               ffprobe_path

    def process(self):
        """
        This takes a 4CAT results file as input, and outputs a new CSV file
        with one column with image hashes, one with the first file name used
        for the image, and one with the amount of times the image was used
        """
        sizing_mode = self.parameters.get("tile-size")
        sort_mode = self.parameters.get("sort-mode")
        sound = self.parameters.get("audio")

        ffmpeg_path = shutil.which(config.get("video_downloader.ffmpeg-path"))
        ffprobe_path = shutil.which("ffprobe".join(ffmpeg_path.rsplit("ffmpeg", 1)))

        # unpack source videos to stack
        # a staging area to store the videos we're reading from
        video_dataset = self.source_dataset.nearest("video-downloader*")
        video_staging_area = video_dataset.get_staging_area()

        lengths = {}
        dimensions = {}
        videos = {}
        longest_index = 0

        # unpack videos and determine length of the video (for sorting)
        for video in self.iterate_archive_contents(video_dataset.get_results_path(), staging_area=video_staging_area,
                                                   immediately_delete=False):
            if self.interrupted:
                shutil.rmtree(video_staging_area, ignore_errors=True)
                return ProcessorInterruptedException("Interrupted while unpacking videos")

            # skip JSON
            if video.name == '.metadata.json':
                continue

            video_path = shlex.quote(str(video))

            # determine length if needed
            probe_command = [ffprobe_path, "-v", "error", "-select_streams", "v:0", "-show_entries",
                             "stream=width,height,duration", "-of", "csv=p=0", video_path]
            probe = subprocess.run(probe_command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE)

            probe_output = probe.stdout.decode("utf-8")
            probe_error = probe.stderr.decode("utf-8")
            if probe_error:
                shutil.rmtree(video_staging_area, ignore_errors=True)
                return self.dataset.finish_with_error("Cannot determine dimensions of video {video.name}. Cannot tile "
                                                      "videos without knowing the video dimensions.")
            else:
                bits = probe_output.split(",")
                dimensions[video.name] = (int(bits[0]), int(bits[1]))
                lengths[video.name] = float(bits[-1])

            videos[video.name] = video

        if sort_mode in ("longest", "shortest"):
            videos = {k: videos[k] for k in
                      sorted(videos, key=lambda k: reduce(mul, dimensions[k]), reverse=(sort_mode == "longest"))}
        elif sort_mode == "random":
            videos = {k: videos[k] for k in sorted(videos, key=lambda k: random.random())}

        # see which of the videos is the longest, after sorting
        # used to determine which audio stream to use
        max_length = max(lengths.values())
        longest_index = 0
        for video in videos:
            if lengths[video] == max_length:
                break

            longest_index += 1

        average_size = (
            sum([k[0] for k in dimensions.values()]) / len(dimensions),
            sum([k[1] for k in dimensions.values()]) / len(dimensions))

        self.dataset.update_status("Determining canvas and tile sizes")

        # calculate 'tile sizes' (a tile is an image) and also the size of the
        # canvas we will need to fit them all. The canvas can never be larger than
        # this:
        max_pixels = self.TARGET_WIDTH * self.TARGET_HEIGHT

        if sizing_mode == "fit-height":
            # assuming every image has the overall average height, how wide would
            # the canvas need to be (if everything is on a single row)?
            full_width = 0
            tile_y = average_size[1]
            for dimension in dimensions.values():
                # ideally, we make everything the average height
                optimal_ratio = average_size[1] / dimension[1]
                full_width += dimension[0] * optimal_ratio

            # now we can calculate the total amount of pixels needed
            fitted_pixels = full_width * tile_y
            if fitted_pixels > max_pixels:
                # try again with a lower height
                area_ratio = max_pixels / fitted_pixels
                tile_y = int(tile_y * math.sqrt(area_ratio))
                fitted_pixels = max_pixels

            # find the canvas size that can fit this amount of pixels at the
            # required proportions, provided that y = multiple of avg height
            ideal_height = math.sqrt(fitted_pixels / (self.TARGET_WIDTH / self.TARGET_HEIGHT))
            size_y = math.ceil(ideal_height / tile_y) * tile_y
            size_x = fitted_pixels / size_y

            tile_x = -1  # varies

        elif sizing_mode == "square":
            # assuming each image is square, find a canvas with the right
            # proportions that would fit all of them
            # assume the average dimensions
            tile_size = int(sum(average_size) / 2)

            # this is how many pixels we need
            fitted_pixels = tile_size * tile_size * len(videos)

            # does that fit our canvas?
            if fitted_pixels > max_pixels:
                tile_size = math.floor(math.sqrt(max_pixels / len(videos)))
                fitted_pixels = tile_size * tile_size * len(videos)

            ideal_width = math.sqrt(fitted_pixels / (self.TARGET_HEIGHT / self.TARGET_WIDTH))
            size_x = math.ceil(ideal_width / tile_size) * tile_size
            size_y = math.ceil(fitted_pixels / size_x / tile_size) * tile_size

            tile_x = tile_y = tile_size

        elif sizing_mode == "average":
            tile_x = int(average_size[0])
            tile_y = int(average_size[1])

            fitted_pixels = tile_x * tile_y * len(videos)
            if fitted_pixels > max_pixels:
                area_ratio = max_pixels / fitted_pixels
                tile_x = int(tile_x * math.sqrt(area_ratio))
                tile_y = int(tile_y * math.sqrt(area_ratio))
                fitted_pixels = tile_x * tile_y * len(videos)

            ideal_width = math.sqrt(fitted_pixels / (self.TARGET_HEIGHT / self.TARGET_WIDTH))
            size_x = math.ceil(ideal_width / tile_x) * tile_x
            size_y = math.ceil(fitted_pixels / size_x / tile_y) * tile_y

        else:
            raise NotImplementedError("Sizing mode '%s' not implemented" % sizing_mode)

        self.dataset.log("Canvas size is %ix%i" % (size_x, size_y))

        # now we are ready to render the video wall
        command = [ffmpeg_path, "-y", "-hide_banner", "-loglevel", "error"]
        # construct an ffmpeg filter for this
        # basically, stack videos vertically until the max width is reached
        # then stack those vertical stacks horizontally
        # resize the videos first
        index = 0
        resize = []  # resize filters to make tiles from videos
        padding = []  # padding filters to make all rows the same width

        row = []
        rows = []
        row_width = 0
        row_widths = []

        # go through each video and transform as needed
        for video, path in videos.items():
            if tile_x < 0:
                video_width = dimensions[video][0] * (tile_y / dimensions[video][1])
            else:
                video_width = tile_x

            row_width += video_width

            # make into tile - with resizing (if proportional) or cropping (if not)
            if sizing_mode == "fit-height":
                resize.append(f"scale={video_width}:{tile_y}[scaled{index}]")
            elif sizing_mode in ("square", "average"):
                if dimensions[video][0] > dimensions[video][1]:
                    cropscale = f"scale={dimensions[video][0] * (tile_y / dimensions[video][1])}:{tile_y}[cropped{index}]"
                else:
                    cropscale = f"scale={tile_x}:{dimensions[video][1] * (tile_x / dimensions[video][0])}[cropped{index}]"

                cropscale += f";[cropped{index}]crop={tile_x}:{tile_y}"
                resize.append(f"[{index}]{cropscale}[scaled{index}]")

            command += ["-i", str(path)]
            if row_width + video_width >= size_x or (index == len(videos) - 1 and row):
                if len(row) > 1:
                    # use hstack to tile the videos in the row horizontally
                    rows.append("".join(row) + f"hstack=inputs={len(row)}[stack{len(rows)}]")
                else:
                    # hstack needs more than one video as input, but we need
                    # *something* to rename the stream to stack[whatever]
                    # so just pad it to the size it already is
                    rows.append(row[0] + f"pad={row_width}:{tile_y}:0:0[stack{len(rows)}]")
                row = []
                row_widths.append(row_width)
                row_width = 0

            row.append(f"[scaled{index}]")

            index += 1

        for row, width in enumerate(row_widths):
            if width != max(row_widths):
                # pad so that each row is the same width
                padding.append(f"[stack{row}]pad={max(row_widths)}:{tile_y}:0:0[stack{row}]")

        # now create the ffmpeg filter from this
        filter_chain = ";".join(resize) + ";"
        filter_chain += ";".join(rows) + ";"
        filter_chain += ";".join(padding) + ";"
        filter_chain += "".join([f"[stack{i}]" for i in range(0, len(rows))]) + f"vstack=inputs={len(rows)}[final]"
        ffmpeg_filter = shlex.quote(filter_chain)[1:-1]
        command += ["-fps_mode", "drop", "-filter_complex", ffmpeg_filter]

        # ensure mixed audio
        if sound == "none":
            command += ["-an"]
        elif sound == "longest":
            command += ["-map", f"{longest_index}:a"]

        # use tiled video stream
        command += ["-map", "[final]"]

        # set output file
        command.append(shlex.quote(str(self.dataset.get_results_path())))
        self.dataset.log(f"Using ffmpeg filter {ffmpeg_filter}")

        if self.interrupted:
            shutil.rmtree(video_staging_area, ignore_errors=True)
            return ProcessorInterruptedException("Interrupted while tiling videos")

        self.dataset.update_status("Merging video files with ffmpeg (this can take a while)")
        result = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        # Capture logs
        ffmpeg_output = result.stdout.decode("utf-8")
        ffmpeg_error = result.stderr.decode("utf-8")

        if ffmpeg_output:
            self.dataset.log("ffmpeg returned the following output:")
            for line in ffmpeg_output.split("\n"):
                self.dataset.log("  " + line)

        if ffmpeg_error:
            self.dataset.log("ffmpeg returned the following errors:")
            for line in ffmpeg_error.split("\n"):
                self.dataset.log("  " + line)

        shutil.rmtree(video_staging_area, ignore_errors=True)

        if result.returncode != 0:
            return self.dataset.finish_with_error(
                f"Could not make video wall (error {result.returncode}); check the dataset log for details.")

        self.dataset.finish(1)
