"""Mapping the image plane onto the ground plane.

The contact point of the wheel lies *on* the ground at the instant of
touchdown, so a plane homography is geometrically exact there — which is also
why the estimator must find the wheel, never the fuselage centroid: a point
0.8 m up is wrong by metres through the same mapping (docs/design.md 4.1).
"""
