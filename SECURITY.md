# Security policy

## Reporting a vulnerability

Please report security problems privately rather than in a public issue.

Use GitHub's private vulnerability reporting on this repository: go to the **Security** tab,
then **Report a vulnerability**. That opens a private advisory visible only to the maintainer.

Please include what the issue allows an attacker to do, the steps to reproduce it, and the
version or commit you tested. You can expect an acknowledgement within a week. If the report
is valid, the fix and the advisory will be published together, and you will be credited unless
you prefer otherwise.

Please do not open a public issue, post the details anywhere public, or test against machines
or repositories you do not own, before the fix is out.

## Supported versions

This project is at an early version and only the latest release is supported. There is no
backporting to earlier tags.

## Scope

This tool allocates large buffers, pins threads, optionally compiles a small
C kernel, and reads system state. The things worth reporting are: injection through a
configuration value or an Action input, unsafe handling of the compiler invocation or the
compiled library's path, and anything that writes outside the paths it is asked to write to.

Reports about the behaviour of the operating system, the kernel, GitHub, or a third-party
service that this tool merely reads from are out of scope here, though they are still worth
telling me about so the documentation can warn people.
