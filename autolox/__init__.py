"""autolox — bulk NFC card enrolment for Loxone Miniservers.

Small async client focused on the enrolment workflow: discover NFC Code Touch
readers, arm learn mode, receive tag IDs from the reader's state stream, bind
each tag to a Loxone user via addusernfc.
"""
