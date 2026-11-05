import SwiftUI

/// The period picker at the top of the screen: Day, Week, Month, Custom,
/// and under it the resolved range with buttons for the period before
/// and after.
///
/// It only changes `query`. The screen reloads when `query` changes.
struct PeriodBar: View {
    @Binding var query: PeriodQuery
    /// The server's echo of the period on screen, or nil before the
    /// first answer. The range text comes only from here (WHO-13 R-44).
    let period: ResolvedPeriod?
    let isLoading: Bool
    /// Called when the user picks Custom, or taps the range of a custom
    /// period to change it.
    let editCustomRange: () -> Void

    var body: some View {
        VStack(spacing: Theme.Spacing.s) {
            Picker("Period", selection: kindSelection) {
                Text("Day").tag(PeriodQuery.Kind.day)
                Text("Week").tag(PeriodQuery.Kind.week)
                Text("Month").tag(PeriodQuery.Kind.month)
                Text("Custom").tag(PeriodQuery.Kind.custom)
            }
            .pickerStyle(.segmented)

            HStack(spacing: Theme.Spacing.s) {
                stepButton(by: -1, systemImage: "chevron.left", label: "Previous \(unitName)")
                Spacer(minLength: 0)
                rangeLabel
                Spacer(minLength: 0)
                stepButton(by: 1, systemImage: "chevron.right", label: "Next \(unitName)")
                    .disabled(period?.isPartial ?? true)
            }
        }
        // The bar stays on screen while the list scrolls, so it stops
        // growing at a size that still leaves room for the list. The
        // list itself scales to the largest size.
        .dynamicTypeSize(...DynamicTypeSize.accessibility2)
        .padding(.horizontal, Theme.Spacing.l)
        .padding(.top, Theme.Spacing.s)
        .padding(.bottom, Theme.Spacing.xs)
        .background(Color(.systemBackground))
        .overlay(alignment: .bottom) { Divider() }
    }

    /// Day, week and month apply at once, starting from the current one.
    /// Custom does not change the query: it opens the date sheet, and the
    /// query changes only when the server accepts the dates.
    private var kindSelection: Binding<PeriodQuery.Kind> {
        Binding(
            get: { query.kind },
            set: { kind in
                if kind == .custom {
                    editCustomRange()
                } else {
                    query = PeriodQuery(kind: kind)
                }
            })
    }

    @ViewBuilder
    private var rangeLabel: some View {
        let text = Text(rangeText)
            .font(.headline)
            .monospacedDigit()
            .multilineTextAlignment(.center)
            .lineLimit(2)
            .minimumScaleFactor(0.8)
            // While the next period loads, the old dates fade so they do
            // not read as the answer.
            .opacity(isLoading ? 0.5 : 1)
        if query.kind == .custom {
            Button(action: editCustomRange) { text }
                .frame(minHeight: 44)
                .accessibilityLabel("\(rangeText). Change dates")
        } else {
            text
                .frame(minHeight: 44)
                .accessibilityLabel(rangeText)
        }
    }

    private var rangeText: String {
        guard let period else { return " " }
        let range = DisplayFormat.periodRange(period)
        return period.isPartial ? "\(range) · so far" : range
    }

    /// Steps one period back or forward with `offset` (R-40). Custom
    /// ranges have no step; the buttons stay in place but are hidden.
    private func stepButton(by step: Int, systemImage: String, label: String) -> some View {
        Button {
            query = PeriodQuery(kind: query.kind, offset: query.offset + step)
        } label: {
            Image(systemName: systemImage)
                .font(.body.weight(.semibold))
                .frame(width: 44, height: 44)
                .contentShape(Rectangle())
        }
        .accessibilityLabel(label)
        .opacity(query.kind == .custom ? 0 : 1)
        .disabled(query.kind == .custom)
        .accessibilityHidden(query.kind == .custom)
    }

    private var unitName: String {
        switch query.kind {
        case .day: return "day"
        case .week: return "week"
        case .month: return "month"
        case .custom: return "range"
        }
    }
}
