import logging
from datetime import datetime, timedelta
from urllib.parse import urlparse

import av
import make87
from make87_messages.core.header_pb2 import Header
from make87_messages.video.frame_h264_pb2 import FrameH264
from onvif import ONVIFCamera


def parse_onvif_url(url):
    parsed = urlparse(url)
    protocol = parsed.scheme
    ip = parsed.hostname
    port = parsed.port  # Will be None if not specified in the URL
    url_suffix = parsed.path  # The part after the IP and port

    return protocol, ip, port, url_suffix


def main():
    make87.initialize()
    topic = make87.get_publisher(name="VIDEO_DATA", message_type=FrameH264)
    onvif_url = make87.resolve_peripheral_name("CAMERA")

    # Example usage:
    protocol, ip, port, url_suffix = parse_onvif_url(onvif_url)

    logging.debug("Protocol:", protocol)
    logging.debug("IP:", ip)
    logging.debug("Port:", port)
    logging.debug("URL Suffix:", url_suffix)

    camera = ONVIFCamera(
        ip, port, make87.get_config_value("CAMERA_USERNAME"), make87.get_config_value("CAMERA_PASSWORD")
    )

    # --- Get the streaming URI via the Media service ---
    # Create the media service client.
    media_service = camera.create_media_service()

    # Retrieve available profiles (video configurations)
    profiles = media_service.GetProfiles()
    if not profiles:
        raise Exception("No media profiles found!")
    default_profile = profiles[0]

    # Create a request to get the stream URI.
    stream_req = media_service.create_type("GetStreamUri")
    stream_req.ProfileToken = default_profile.token
    stream_req.StreamSetup = {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}}

    stream_uri = media_service.GetStreamUri(stream_req).Uri
    logging.info(f"Stream URI: {stream_uri}")

    container = av.open(stream_uri)
    stream_start = datetime.now()  # Unix timestamp in seconds
    video_stream = next((s for s in container.streams if s.type == "video"), None)

    if video_stream is None:
        raise Exception("No video stream found.")

    frame_buffer = bytearray()
    current_pts = None

    for packet in container.demux(video_stream):
        # Use PTS if available, otherwise fallback to DTS
        pts = packet.pts if packet.pts is not None else packet.dts
        if pts is None:
            continue  # Skip packets with no timestamp

        # If we're starting a new frame (different timestamp) and have data buffered:
        if current_pts is not None and pts != current_pts:
            # Compute the relative timestamp in seconds.
            relative_timestamp = float(current_pts * video_stream.time_base)
            # Add the relative timestamp to the stream start time.
            absolute_timestamp = stream_start + timedelta(seconds=relative_timestamp)

            header = Header(entity_path=f"/camera/{ip}")
            header.timestamp.FromDatetime(absolute_timestamp)
            message = FrameH264(header=header, data=bytes(frame_buffer))
            topic.publish(message)

            frame_buffer = bytearray()

        current_pts = pts
        frame_buffer.extend(bytes(packet))


if __name__ == "__main__":
    main()
