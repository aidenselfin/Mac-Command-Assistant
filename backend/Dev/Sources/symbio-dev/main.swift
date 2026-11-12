import Foundation
import SymbioDev

// One subcommand per former script.
let arguments = Array(CommandLine.arguments.dropFirst())
let rest = Array(arguments.dropFirst())
let code: Int32
switch arguments.first {
case "vibe": code = Vibe.main(rest)
case "heavy-lock": code = HeavyLock.main(rest)
case "ui-check": code = UICheck.main(rest)
case "eval": code = await Eval.main(rest)
case "benchmark": code = Benchmark.main(rest)
case "make-cases": code = await MakeCases.main(rest)
case "check": code = Check.main(rest)
default:
    writeError("usage: symbio-dev {vibe,heavy-lock,ui-check,eval,benchmark,make-cases,check} ...\n")
    code = 2
}
exit(code)
