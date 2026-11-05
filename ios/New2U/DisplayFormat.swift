import Foundation

/// Turns dates and durations into text.
///
/// Times and dates use the **stored display zone** that the server
/// echoes in `period.time_zone`, never the device's zone (WHO-12 D-4,
/// WHO-13 R-6). Only the locale (word order, 12/24 h, month names) comes
/// from the device. `locale` is a parameter only so the tests can fix it.
enum DisplayFormat {
    /// "21:14" or "9:14 PM": the clock time of a play in the display zone.
    static func clockTime(_ date: Date, timeZone: TimeZone, locale: Locale = .current) -> String {
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = timeZone
        formatter.dateStyle = .none
        formatter.timeStyle = .short
        return formatter.string(from: date)
    }

    /// The day header: "Today", "Yesterday", or a date such as
    /// "Sunday 11 October" in the device's locale.
    ///
    /// `localDate` is the server's "YYYY-MM-DD". "Today" is read in the
    /// display zone, so the label matches the date under it.
    static func dayHeader(
        localDate: String,
        timeZone: TimeZone,
        locale: Locale = .current,
        now: Date = Date()
    ) -> String {
        guard let date = middayOf(localDate: localDate, timeZone: timeZone) else { return localDate }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        if calendar.isDate(date, inSameDayAs: now) {
            return String(localized: "Today")
        }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
           calendar.isDate(date, inSameDayAs: yesterday) {
            return String(localized: "Yesterday")
        }
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = timeZone
        formatter.setLocalizedDateFormatFromTemplate("EEEEdMMMM")
        return formatter.string(from: date)
    }

    /// "30 March": a deadline date in the display zone, device locale.
    static func dayAndMonth(_ date: Date, timeZone: TimeZone, locale: Locale = .current) -> String {
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = timeZone
        formatter.setLocalizedDateFormatFromTemplate("dMMMM")
        return formatter.string(from: date)
    }

    /// "10 September 2026": the collection start date in V1. Read from
    /// the server's "YYYY-MM-DD", device locale.
    static func longDate(localDate: String, timeZone: TimeZone, locale: Locale = .current) -> String {
        guard let date = middayOf(localDate: localDate, timeZone: timeZone) else { return localDate }
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = timeZone
        formatter.setLocalizedDateFormatFromTemplate("dMMMMy")
        return formatter.string(from: date)
    }

    /// "5 Oct", or "5 Oct 2025" outside the current year: the date of a
    /// first play in a "New to you" row. Read from the server's
    /// "YYYY-MM-DD", never worked out from the instant on the phone.
    static func shortDate(
        localDate: String,
        timeZone: TimeZone,
        locale: Locale = .current,
        now: Date = Date()
    ) -> String {
        guard let date = middayOf(localDate: localDate, timeZone: timeZone) else { return localDate }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        let showsYear = calendar.component(.year, from: date) != calendar.component(.year, from: now)
        let formatter = DateFormatter()
        formatter.locale = locale
        formatter.timeZone = timeZone
        formatter.setLocalizedDateFormatFromTemplate(showsYear ? "dMMMy" : "dMMM")
        return formatter.string(from: date)
    }

    /// A zone's name for Settings: "Central European Time" in the
    /// device's language. Falls back to the IANA id if there is no name.
    static func zoneName(_ zone: TimeZone, locale: Locale = .current) -> String {
        zone.localizedName(for: .generic, locale: locale) ?? zone.identifier
    }

    /// True when two zones are different places, so Settings shows the
    /// device's zone next to the stored one. Compared by id, not by the
    /// current offset: two zones can share an offset today and not after
    /// a DST change.
    static func isDifferentZone(_ a: TimeZone, _ b: TimeZone) -> Bool {
        a.identifier != b.identifier
    }

    /// "5 min ago": how long ago the server last read plays from Spotify.
    static func lastChecked(_ date: Date, locale: Locale = .current, now: Date = Date()) -> String {
        let formatter = RelativeDateTimeFormatter()
        formatter.locale = locale
        formatter.unitsStyle = .short
        // "now" rather than "in 0 sec" when the times match.
        formatter.dateTimeStyle = .named
        return formatter.localizedString(for: min(date, now), relativeTo: now)
    }

    /// A track length for the row: "3m 42s".
    static func duration(milliseconds: Int, locale: Locale = .current) -> String {
        let formatter = DateComponentsFormatter()
        formatter.unitsStyle = .abbreviated
        formatter.allowedUnits = milliseconds >= 3_600_000 ? [.hour, .minute, .second] : [.minute, .second]
        formatter.zeroFormattingBehavior = .dropLeading
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = locale
        formatter.calendar = calendar
        return formatter.string(from: TimeInterval(milliseconds / 1000)) ?? ""
    }

    /// A day or period total: "3h 12m". Under a minute shows "0m".
    static func total(milliseconds: Int, locale: Locale = .current) -> String {
        let formatter = DateComponentsFormatter()
        formatter.unitsStyle = .abbreviated
        formatter.allowedUnits = [.hour, .minute]
        formatter.zeroFormattingBehavior = .dropLeading
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = locale
        formatter.calendar = calendar
        return formatter.string(from: TimeInterval(milliseconds / 1000)) ?? ""
    }

    /// The same length, spelled out for VoiceOver: "3 minutes, 42 seconds".
    static func spokenDuration(milliseconds: Int, locale: Locale = .current) -> String {
        let formatter = DateComponentsFormatter()
        formatter.unitsStyle = .full
        formatter.allowedUnits = [.hour, .minute, .second]
        formatter.zeroFormattingBehavior = .dropAll
        var calendar = Calendar(identifier: .gregorian)
        calendar.locale = locale
        formatter.calendar = calendar
        return formatter.string(from: TimeInterval(milliseconds / 1000)) ?? ""
    }

    /// The resolved period under the picker: "Today", "5–11 Oct",
    /// "October 2026". Built only from the server's echo (WHO-13 R-44,
    /// R-45); the app never works out a period's dates itself.
    static func periodRange(
        _ period: ResolvedPeriod,
        locale: Locale = .current,
        now: Date = Date()
    ) -> String {
        let zone = TimeZone(identifier: period.timeZone) ?? .current
        if period.type == "day" {
            return dayHeader(localDate: period.localStartDate, timeZone: zone, locale: locale, now: now)
        }
        guard let first = middayOf(localDate: period.localStartDate, timeZone: zone),
              let last = middayOf(localDate: period.localEndDate, timeZone: zone)
        else { return "\(period.localStartDate) – \(period.localEndDate)" }

        let formatter = DateIntervalFormatter()
        formatter.locale = locale
        formatter.timeZone = zone
        if period.type == "month" {
            formatter.dateTemplate = "MMMMy"
            return formatter.string(from: first, to: first)
        }
        // Show the year only when the range is not in the current year.
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = zone
        let thisYear = calendar.component(.year, from: now)
        let showsYear = calendar.component(.year, from: first) != thisYear
            || calendar.component(.year, from: last) != thisYear
        formatter.dateTemplate = showsYear ? "dMMMy" : "dMMM"
        return formatter.string(from: first, to: last)
    }

    /// A picked date as the server's "YYYY-MM-DD", read in the display zone.
    static func localDate(_ date: Date, timeZone: TimeZone) -> String {
        let formatter = DateFormatter()
        formatter.locale = Locale(identifier: "en_US_POSIX")
        formatter.calendar = Calendar(identifier: .gregorian)
        formatter.timeZone = timeZone
        formatter.dateFormat = "yyyy-MM-dd"
        return formatter.string(from: date)
    }

    /// Reads "YYYY-MM-DD" as noon on that date in the display zone.
    /// Noon, because a few zones skip midnight on their DST change day.
    static func middayOf(localDate: String, timeZone: TimeZone) -> Date? {
        let parts = localDate.split(separator: "-").compactMap { Int($0) }
        guard parts.count == 3 else { return nil }
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = timeZone
        return calendar.date(from: DateComponents(year: parts[0], month: parts[1], day: parts[2], hour: 12))
    }
}
