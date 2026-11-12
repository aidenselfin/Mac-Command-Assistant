import Foundation

/// Minimal draft 2020-12 validator covering the keywords Symbio's tool schemas use:
/// type, properties, required, additionalProperties, items, enum, const, anyOf, oneOf,
/// minLength, maxLength, minItems, maxItems, uniqueItems, minimum, maximum, pattern.
/// ponytail: no $ref/allOf/format; add when a schema needs them.
public enum JSONSchema {
    public static func isValid(_ value: JSON, schema: JSON) -> Bool {
        if case let .bool(allowed) = schema { return allowed }
        guard schema.objectValue != nil else { return true }
        if let type = schema["type"] {
            let names = type.arrayValue?.compactMap(\.stringValue) ?? [type.stringValue].compactMap { $0 }
            if !names.contains(where: { matches(value, type: $0) }) { return false }
        }
        if let options = schema["enum"]?.arrayValue, !options.contains(where: { same($0, value) }) { return false }
        if let constant = schema["const"], !same(constant, value) { return false }
        if let any = schema["anyOf"]?.arrayValue, !any.contains(where: { isValid(value, schema: $0) }) { return false }
        if let one = schema["oneOf"]?.arrayValue, one.filter({ isValid(value, schema: $0) }).count != 1 { return false }
        switch value {
        case let .string(s):
            let length = s.unicodeScalars.count
            if let min = schema["minLength"]?.intValue, length < min { return false }
            if let max = schema["maxLength"]?.intValue, length > max { return false }
            if let pattern = schema["pattern"]?.stringValue {
                guard let regex = try? NSRegularExpression(pattern: pattern) else { return false }
                if regex.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) == nil { return false }
            }
        case let .array(items):
            if let min = schema["minItems"]?.intValue, items.count < min { return false }
            if let max = schema["maxItems"]?.intValue, items.count > max { return false }
            if schema["uniqueItems"]?.boolValue == true {
                for (i, item) in items.enumerated() where items[..<i].contains(where: { same($0, item) }) { return false }
            }
            if let itemSchema = schema["items"], !items.allSatisfy({ isValid($0, schema: itemSchema) }) { return false }
        case .int, .double:
            let number = value.doubleValue!
            if let min = schema["minimum"]?.doubleValue, number < min { return false }
            if let max = schema["maximum"]?.doubleValue, number > max { return false }
        case let .object(pairs):
            let properties = schema["properties"]?.objectValue ?? []
            for name in schema["required"]?.arrayValue?.compactMap(\.stringValue) ?? []
            where !pairs.contains(where: { $0.0 == name }) { return false }
            for (key, item) in pairs {
                if let sub = properties.first(where: { $0.0 == key })?.1 {
                    if !isValid(item, schema: sub) { return false }
                } else if let extra = schema["additionalProperties"], !isValid(item, schema: extra) {
                    return false
                }
            }
        default: break
        }
        return true
    }

    static func matches(_ value: JSON, type: String) -> Bool {
        switch (type, value) {
        case ("null", .null), ("boolean", .bool), ("string", .string), ("array", .array), ("object", .object),
             ("integer", .int), ("number", .int), ("number", .double): true
        case let ("integer", .double(d)): d == d.rounded()
        default: false
        }
    }

    /// JSON Schema equality: true != 1 (unlike Python ==).
    static func same(_ a: JSON, _ b: JSON) -> Bool {
        switch (a, b) {
        case (.bool, .int), (.int, .bool), (.bool, .double), (.double, .bool): false
        default: a == b
        }
    }
}
