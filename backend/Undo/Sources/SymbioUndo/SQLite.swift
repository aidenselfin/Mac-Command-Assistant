import Foundation
import SQLite3

/// One SQLite cell. Python `sqlite3` maps these to None/int/float/str/bytes.
public enum SQLiteValue: Equatable, Sendable {
    case null
    case integer(Int64)
    case real(Double)
    case text(String)
    case blob(Data)

    public var isNull: Bool { self == .null }
    public var intValue: Int64? { if case let .integer(v) = self { return v }; return nil }
    public var stringValue: String? { if case let .text(v) = self { return v }; return nil }
    public var dataValue: Data? { if case let .blob(v) = self { return v }; return nil }
}

public struct SQLiteError: Error, Equatable, Sendable, CustomStringConvertible {
    public let code: Int32
    public let message: String
    public var description: String { "SQLite error \(code): \(message)" }
}

/// Thin `import SQLite3` wrapper shared by the journal packages (Undo, Delete, FileOps, Feedback...).
/// Not thread-safe by design: open one per unit of work, like Python's `sqlite3.connect` per `_connect`.
public final class SQLiteDatabase {
    public enum TransactionMode: String, Sendable {
        case deferred = "DEFERRED", immediate = "IMMEDIATE", exclusive = "EXCLUSIVE"
    }

    private let handle: OpaquePointer

    /// `timeout` matches Python `sqlite3.connect(timeout=)` (busy wait in seconds).
    public init(path: String, timeout: TimeInterval = 5) throws {
        var db: OpaquePointer?
        let code = sqlite3_open_v2(
            path, &db, SQLITE_OPEN_READWRITE | SQLITE_OPEN_CREATE | SQLITE_OPEN_FULLMUTEX, nil
        )
        guard code == SQLITE_OK, let db else {
            let message = db.map { String(cString: sqlite3_errmsg($0)) } ?? "out of memory"
            sqlite3_close_v2(db)
            throw SQLiteError(code: code, message: message)
        }
        handle = db
        sqlite3_busy_timeout(db, Int32(timeout * 1000))
    }

    deinit { sqlite3_close_v2(handle) }

    /// Runs one statement and returns the changed row count (Python `cursor.rowcount` for DML).
    @discardableResult
    public func execute(_ sql: String, _ parameters: [SQLiteValue] = []) throws -> Int {
        _ = try query(sql, parameters)
        return Int(sqlite3_changes(handle))
    }

    /// Runs one statement and returns every row.
    public func query(_ sql: String, _ parameters: [SQLiteValue] = []) throws -> [[SQLiteValue]] {
        var prepared: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &prepared, nil) == SQLITE_OK, let statement = prepared else {
            throw lastError()
        }
        defer { sqlite3_finalize(statement) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        for (offset, value) in parameters.enumerated() {
            let index = Int32(offset + 1)
            let code: Int32 = switch value {
            case .null: sqlite3_bind_null(statement, index)
            case let .integer(v): sqlite3_bind_int64(statement, index, v)
            case let .real(v): sqlite3_bind_double(statement, index, v)
            case let .text(v): sqlite3_bind_text(statement, index, v, -1, transient)
            case let .blob(v) where v.isEmpty: sqlite3_bind_zeroblob(statement, index, 0)
            case let .blob(v): v.withUnsafeBytes {
                    sqlite3_bind_blob(statement, index, $0.baseAddress, Int32($0.count), transient)
                }
            }
            guard code == SQLITE_OK else { throw lastError() }
        }
        var rows: [[SQLiteValue]] = []
        while true {
            let code = sqlite3_step(statement)
            if code == SQLITE_DONE { return rows }
            guard code == SQLITE_ROW else { throw lastError() }
            rows.append((0..<sqlite3_column_count(statement)).map { column(statement, $0) })
        }
    }

    /// `BEGIN <mode>` … `COMMIT`, rolled back when `body` throws (Python `_connect` context).
    public func transaction<T>(
        _ mode: TransactionMode = .deferred, _ body: (SQLiteDatabase) throws -> T
    ) throws -> T {
        try execute("BEGIN \(mode.rawValue)")
        do {
            let result = try body(self)
            try execute("COMMIT")
            return result
        } catch {
            if sqlite3_get_autocommit(handle) == 0 { _ = try? execute("ROLLBACK") }
            throw error
        }
    }

    private func column(_ statement: OpaquePointer, _ index: Int32) -> SQLiteValue {
        switch sqlite3_column_type(statement, index) {
        case SQLITE_INTEGER: return .integer(sqlite3_column_int64(statement, index))
        case SQLITE_FLOAT: return .real(sqlite3_column_double(statement, index))
        case SQLITE_TEXT:
            guard let text = sqlite3_column_text(statement, index) else { return .text("") }
            let count = Int(sqlite3_column_bytes(statement, index))
            return .text(String(decoding: UnsafeBufferPointer(start: text, count: count), as: UTF8.self))
        case SQLITE_BLOB:
            let count = Int(sqlite3_column_bytes(statement, index))
            guard let bytes = sqlite3_column_blob(statement, index), count > 0 else { return .blob(Data()) }
            return .blob(Data(bytes: bytes, count: count))
        default: return .null
        }
    }

    private func lastError() -> SQLiteError {
        SQLiteError(code: sqlite3_errcode(handle), message: String(cString: sqlite3_errmsg(handle)))
    }
}
