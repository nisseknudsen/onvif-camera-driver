import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse

import av
import make87
from make87_messages.core.header_pb2 import Header
from make87_messages.video.any_pb2 import FrameAny
from make87_messages.video.frame_av1_pb2 import FrameAV1
from make87_messages.video.frame_h264_pb2 import FrameH264
from make87_messages.video.frame_h265_pb2 import FrameH265
from onvif import ONVIFCamera
from urllib.parse import urlparse, urlunparse

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def parse_url(url):
    parsed = urlparse(url)
    protocol = parsed.scheme
    ip = parsed.hostname
    port = parsed.port  # Will be None if not specified in the URL
    url_suffix = parsed.path  # The part after the IP and port

    return protocol, ip, port, url_suffix


def inject_rtsp_auth(uri: str, username: str, password: str) -> str:
    parsed = urlparse(uri)

    netloc_with_auth = f"{username}:{password}@{parsed.hostname}"
    if parsed.port:
        netloc_with_auth += f":{parsed.port}"

    return urlunparse(
        (
            parsed.scheme,
            netloc_with_auth,
            parsed.path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )


# Generic function for encoding frames
def encode_frame(codec, header, packet: av.Packet, width: int, height: int) -> FrameAny:
    codec_classes = {
        "h264": ("h264", FrameH264),
        "hevc": ("h265", FrameH265),
        "av1": ("av1", FrameAV1),
    }

    if codec not in codec_classes:
        raise ValueError(f"Unsupported codec: {codec}")

    codec_field, codec_class = codec_classes[codec]
    sub_message = codec_class(
        header=header,
        data=bytes(packet),
        width=width,
        height=height,
        is_keyframe=packet.is_keyframe,
        pts=packet.pts,
        dts=packet.dts,
        duration=packet.duration,
        time_base=codec_class.Fraction(
            num=packet.time_base.numerator,
            den=packet.time_base.denominator,
        ),
    )

    return FrameAny(header=header, **{codec_field: sub_message})


def check_annex_b_format(packet: av.Packet):
    """
    Check if the packet is in Annex B format.
    This is typically used for H.264 streams.
    """
    # Check if the packet starts with the Annex B start code
    data = bytes(packet)  # get the raw packet bytes
    if not (data.startswith(b"\x00\x00\x00\x01") or data.startswith(b"\x00\x00\x01")):
        raise NotImplementedError(
            "Only Annex B format is supported for H.264/H.265 streams."
        )


def main():
    make87.initialize()
    topic = make87.get_publisher(name="VIDEO_DATA", message_type=FrameAny)

    onvif_url = make87.resolve_peripheral_name("ONVIF_DEVICE")
    username, password = (
        make87.get_config_value("ONVIF_USERNAME"),
        make87.get_config_value("ONVIF_PASSWORD"),
    )
    profile_index = make87.get_config_value("PROFILE_INDEX", decode=int)

    protocol, ip, port, url_suffix = parse_url(onvif_url)

    camera = ONVIFCamera(host=ip, port=port, user=username, passwd=password)

    # --- Get the streaming URI via the Media service ---
    # Create the media service client.
    media_service = camera.create_media_service()

    # Retrieve available profiles (video configurations)
    profiles = media_service.GetProfiles()
    if len(profiles) < profile_index + 1:
        raise Exception(f"No profile with index {profile_index} available.")
    default_profile = profiles[profile_index]

    logging.debug("Selected Profile:")
    logging.debug(default_profile)

    # Create a request to get the stream URI.
    stream_req = media_service.create_type("GetStreamUri")
    stream_req.ProfileToken = default_profile.token
    stream_req.StreamSetup = {
        "Stream": "RTP-Unicast",
        "Transport": {"Protocol": "RTSP"},
    }

    stream_uri = media_service.GetStreamUri(stream_req).Uri
    logging.info(f"Stream URI: {stream_uri}")

    _, _, _, entity_path = parse_url(url=stream_uri)
    stream_uri = inject_rtsp_auth(uri=stream_uri, username=username, password=password)
    with av.open(stream_uri) as container:
        stream_start = datetime.now()  # Reference timestamp

        # Find the requested video stream
        video_streams = container.streams.video
        if len(video_streams) == 0:
            raise ValueError("No video stream not found.")

        video_stream = video_streams[0]

        # Print stream information
        stream_info = {
            "Index": video_stream.index,
            "Codec": video_stream.codec_context.name,
            "Resolution": f"{video_stream.width}x{video_stream.height}",
            "Pixel Format": video_stream.pix_fmt,
            "Frame Rate": str(video_stream.average_rate),
        }
        logger.info(f"Stream Attributes: {stream_info}")

        # Validate codec support
        codec_name = video_stream.codec_context.name
        if codec_name not in {"h264", "hevc", "av1"}:
            raise ValueError(f"Unsupported codec: {codec_name}")

        # Stream metadata
        start_pts = video_stream.start_time or 0  # Handle missing start_time
        time_base = float(video_stream.time_base)
        width, height = video_stream.width, video_stream.height

        validated_annex_b = False

        for packet in container.demux(video_stream):
            if packet.dts is None:
                continue  # Skip invalid frames

            if not validated_annex_b:
                if codec_name in {"h264", "hevc"}:
                    # Check for Annex B format
                    check_annex_b_format(packet)
                validated_annex_b = True

            # Compute timestamps
            relative_timestamp = (packet.pts - start_pts) * time_base
            absolute_timestamp = stream_start + timedelta(seconds=relative_timestamp)

            header = Header(entity_path=f"/camera/{entity_path.removeprefix('/')}")
            header.timestamp.FromDatetime(absolute_timestamp)

            # Encode and publish the frame
            topic.publish(encode_frame(codec_name, header, packet, width, height))


if __name__ == "__main__":
    main()
