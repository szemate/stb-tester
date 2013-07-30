"""Process audio stream from the sound card of the test executor PC.

As the HDCP stripper converts the HDMI A/V stream to analog A/V channels, it
is possible to connect the audio output of the HDCP stripper to the microphone
input of the built-in sound card of the executor PC (or any other analog audio
interface). This saves us from demultiplexing the video stream and makes it
possible to build up a separate pipeline for audio detection.

Three parameters have to be specified in the stbt config file:
* `audio_source`:
    Set it to `pulsesrc` on UNIX systems that use PulseAudio; it streams audio
    from the default audio interface set in System Settings.
    IMPORTANT: be careful to set the `volume` property of the audio source low
    enough not to have audible distortion in the audio stream.
* `audio_sink`:
    Set it to `autoaudiosink` to direct audio to the default audio output,
    or to `fakesink` to mute it.
* `audio_noise_level`:
    The background noise level of the source audio stream in dB. Its value
    can be determined by running this module as a test script with the
    set-top-box muted.

At the current state the module is capable of detecting whether audio volume
level exceeds a threshold level using the `level` GStreamer element. As audio
detection is used by a small number of tests, unlike the video pipeline, audio
pipeline is not initialised at start but it's built up on request when any of
the public functions of this module is called.
"""

from collections import deque
import Queue

import stbt

with stbt.hide_argv(), stbt.hide_stderr():
    import gst


RMS = "rms"
PEAK = "peak"


class LowAudio(stbt.UITestFailure):
    def __init__(self, levels, threshold_level, timeout_secs):
        super(LowAudio, self).__init__()
        self.levels = levels
        self.threshold_level = threshold_level
        self.timeout_secs = timeout_secs

    def __str__(self):
        return ("Audio level didn't exceed the %f dB threshold within %d "
                "seconds." % (self.threshold_level, self.timeout_secs))


class GlitchDetected(stbt.UITestFailure):
    def __init__(self, levels, threshold_level):
        super(GlitchDetected, self).__init__()
        self.levels = levels
        self.threshold_level = threshold_level

    def __str__(self):
        return ("Audio level exceeded the %f dB threshold level."
                % self.threshold_level)


def wait_for_sound(timeout_secs=10, consecutive_samples="10/20", threshold=20):
    """Detect if there is any sound present in the source audio stream.

    Returns the audio levels by channel when sound is detected.
    Raises `LowAudio` if the RMS level of any of the audio channels doesn't
    exceed `noise_level` by at least `threshold` dB for `consecutive_samples`
    within `timeout_secs` seconds.

    `consecutive_samples` can be:
    * a positive integer value, or
    * a string in the form "x/y", where `x` is the number of samples with sound
      level exceeding the threshold level out of a sliding window of `y`
      samples.
    """
    consecutive_samples = str(consecutive_samples)
    if "/" in consecutive_samples:
        sound_detected_samples = int(consecutive_samples.split("/")[0])
        considered_samples = int(consecutive_samples.split("/")[1])
    else:
        sound_detected_samples = int(consecutive_samples)
        considered_samples = int(consecutive_samples)

    if sound_detected_samples > considered_samples:
        raise stbt.ConfigurationError(
            "`sound_detected_samples` exceeds `considered_samples`")

    threshold_level = threshold + float(stbt.get_config('global',
                                                        'audio_noise_level'))

    stbt.debug("Waiting for %d out of %d audio samples whose mean level "
               "exceed %f dB" % (sound_detected_samples,
                                 considered_samples, threshold_level))

    samples = deque(maxlen=considered_samples)
    for levels in _audio_levels(timeout_secs=timeout_secs, method=RMS):
        samples.append(bool([l for l in levels if l >= threshold_level]))
        if samples.count(True) >= sound_detected_samples:
            stbt.debug("Audio detected.")
            return levels

    raise LowAudio(levels, threshold_level, timeout_secs)


def ensure_no_glitch(timeout_secs=30, threshold=20, min_samples=10):
    """Detect glitches (momentary deviations from the average audio level) in
    the source audio stream.

    Returns after `timeout_secs` if no glitches are detected.
    Raises `GlitchDetected` if the peak audio level on any channel exceeds the
    average peak level by more than `threshold` dB.

    Detection starts after an average peak audio level is determined from
    `min_samples` number of audio samples. The average peak level is being
    refined continuously while the function is running.

    Assumes that the audio channels of the programme being played on the
    set-top-box have been normalised, therefore high audio peaks indicate
    set-top-box defect.
    """
    stbt.debug("Detecting glitches in the audio stream")

    sum_levels = 0
    num_samples = 0
    threshold_level = float("inf")
    for levels in _audio_levels(timeout_secs=timeout_secs, method=PEAK):
        if num_samples > min_samples and [l for l in levels
                                          if l > threshold_level]:
            raise GlitchDetected(levels, threshold_level)
        sum_levels += sum(levels) / len(levels)
        num_samples += 1
        threshold_level = threshold + (sum_levels / num_samples)


def _audio_levels(timeout_secs=10, method=RMS):
    """Generator that yields a tuple of audio levels for each sample of the
    source audio stream; each element of the tuple stores the level of one
    audio channel in dB.

    Returns after `timeout_secs` seconds. (Note that the caller can also choose
    to stop iterating over this function's results at any time.)

    According to `method` (`RMS` or `PEAK`) it returns:
    * the root mean square of the levels of frequencies of the audio
      spectrum, or
    * the highest (peak) value of the levels of frequencies of the audio
      spectrum.
    """
    last_msg = Queue.Queue(maxsize=1)

    def on_message(_, msg):
        if msg.type == gst.MESSAGE_ELEMENT:
            if msg.structure.get_name() == "level":
                last_msg.put(msg)
        return True

    pipeline_description = " ! ".join([
        stbt.get_config("global", "audio_source"),
        "level message=true",
        stbt.get_config("global", "audio_sink")])

    pipeline = gst.parse_launch(pipeline_description)
    pipeline.get_bus().add_watch(on_message)
    pipeline.set_state(gst.STATE_PLAYING)

    start_timestamp = None
    try:
        while True:
            msg = last_msg.get(timeout=2)
            if not start_timestamp:
                start_timestamp = msg.timestamp
            if msg.timestamp - start_timestamp > timeout_secs * 1e9:
                return
            levels = tuple(msg.structure[method])
            stbt.debug(
                "timestamp: %d; %s audio levels: %s" % (
                    msg.timestamp, method,
                    ", ".join(["%.3f" % l for l in levels])))
            yield levels
    finally:
        pipeline.set_state(gst.STATE_NULL)


def _mean_level(timeout_secs=3):
    sum_levels = 0
    num_samples = 0
    for levels in _audio_levels(timeout_secs=timeout_secs, method=RMS):
        sum_levels += sum(levels) / len(levels)
        num_samples += 1
    return (sum_levels / num_samples)


if "wait_for_match" in globals().keys():  # Running via `stbt run`
    print "Determining mean audio level..."
    print "Mean audio level: %.3f dB" % _mean_level()
