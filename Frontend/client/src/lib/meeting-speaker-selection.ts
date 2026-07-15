import type { OutlookCalendarContact } from "./api-types";

export function meetingContactId(contact: OutlookCalendarContact): string {
  return contact.participantId || contact.address.trim().toLocaleLowerCase();
}

export function meetingContactIdentityKeys(contact: OutlookCalendarContact): string[] {
  return [contact.address, ...(contact.aliases ?? []), contact.participantId ?? ""]
    .map((value) => value.trim().toLocaleLowerCase())
    .filter(Boolean);
}

export function meetingContactsMatch(
  left: OutlookCalendarContact,
  right: OutlookCalendarContact,
): boolean {
  const leftKeys = new Set(meetingContactIdentityKeys(left));
  return meetingContactIdentityKeys(right).some((key) => leftKeys.has(key));
}

export function meetingAttendeeIdForContact(
  attendees: readonly OutlookCalendarContact[],
  contact: OutlookCalendarContact | null,
): string {
  if (!contact) return "";
  const keys = new Set(meetingContactIdentityKeys(contact));
  const attendee = attendees.find((candidate) => (
    meetingContactIdentityKeys(candidate).some((key) => keys.has(key))
  ));
  return attendee ? meetingContactId(attendee) : "";
}

/**
 * Preserve an in-progress choice, otherwise map durable/suggested identities
 * onto the exact Outlook attendee id used by the Select component.
 */
export function initialMeetingParticipantId(
  attendees: readonly OutlookCalendarContact[],
  currentId: string,
  confirmed: OutlookCalendarContact | null,
  suggested: OutlookCalendarContact | null,
): string {
  if (currentId && attendees.some((attendee) => meetingContactId(attendee) === currentId)) {
    return currentId;
  }
  return meetingAttendeeIdForContact(attendees, confirmed)
    || meetingAttendeeIdForContact(attendees, suggested)
    || "";
}
