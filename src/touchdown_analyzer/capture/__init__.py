"""Continuous recording and the raw segment index.

The recorder and the analysis worker share only a folder of fixed-length mp4
segments: a dropped frame in the analyser costs a measurement, a dropped frame
in the recorder costs a whole landing. See docs/design.md 1.
"""
