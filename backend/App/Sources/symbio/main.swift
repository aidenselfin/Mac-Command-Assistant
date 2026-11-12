import Foundation
import SymbioApp

// `symbio lessons|transcript|autonomy ...` (also the old `--lessons/--lesson-on/--lesson-off`).
exit(symbioMain(Array(CommandLine.arguments.dropFirst())))
