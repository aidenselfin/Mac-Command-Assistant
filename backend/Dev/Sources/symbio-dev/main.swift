import Foundation
import SymbioDev

// One subcommand per former script. New ones (check, eval, benchmark, make-cases) add a case here.
let arguments = Array(CommandLine.arguments.dropFirst())
let rest = Array(arguments.dropFirst())
let code: Int32
switch arguments.first {
case "vibe": code = Vibe.main(rest)
case "heavy-lock": code = HeavyLock.main(rest)
case "ui-check": code = UICheck.main(rest)
default:
    writeError("usage: symbio-dev {vibe,heavy-lock,ui-check} ...\n")
    code = 2
}
exit(code)
