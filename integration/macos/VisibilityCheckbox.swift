import AppKit
import SwiftUI

enum VisibilityState: Equatable {
    case off, mixed, on

    init(_ values: [Bool]) {
        if values.isEmpty { self = .off }
        else if values.allSatisfy({ $0 }) { self = .on }
        else if values.contains(true) { self = .mixed }
        else { self = .off }
    }

    var nextVisibility: Bool { self != .on }
    var controlState: NSControl.StateValue {
        switch self {
        case .off: return .off
        case .mixed: return .mixed
        case .on: return .on
        }
    }
}

// SwiftUI's Bool Toggle cannot display a partially enabled group. Keep the
// native checkbox state and its click contract together for every provider.
struct VisibilityCheckbox: NSViewRepresentable {
    let state: VisibilityState
    let label: String
    let onToggle: (Bool) -> Void
    @Environment(\.isEnabled) private var isEnabled

    func makeCoordinator() -> Coordinator { Coordinator(state: state, onToggle: onToggle) }

    func makeNSView(context: Context) -> NSButton {
        context.coordinator.makeButton(label: label)
    }

    func updateNSView(_ button: NSButton, context: Context) {
        context.coordinator.update(button, state: state, label: label, enabled: isEnabled, onToggle: onToggle)
    }

    final class Coordinator: NSObject {
        private var state: VisibilityState
        private var onToggle: (Bool) -> Void

        init(state: VisibilityState, onToggle: @escaping (Bool) -> Void) {
            self.state = state
            self.onToggle = onToggle
        }

        func makeButton(label: String) -> NSButton {
            let button = NSButton(checkboxWithTitle: "", target: self, action: #selector(toggle(_:)))
            button.allowsMixedState = true
            update(button, state: state, label: label, enabled: true, onToggle: onToggle)
            return button
        }

        func update(_ button: NSButton, state: VisibilityState, label: String, enabled: Bool, onToggle: @escaping (Bool) -> Void) {
            self.state = state
            self.onToggle = onToggle
            button.state = state.controlState
            button.isEnabled = enabled
            button.setAccessibilityLabel(label)
        }

        @objc private func toggle(_ sender: NSButton) {
            let visible = state.nextVisibility
            state = visible ? .on : .off
            sender.state = state.controlState
            onToggle(visible)
        }
    }
}
