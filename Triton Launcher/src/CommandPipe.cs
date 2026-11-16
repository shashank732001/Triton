using System;
using System.IO;
using System.IO.Pipes;
using System.Threading;

namespace TritonLauncher
{
    /// <summary>
    /// Local named pipe that accepts one text command per line ("launch chrome", "volume up", "home").
    /// Used by a second TritonLauncher.exe instance today, and by a voice assistant later.
    /// </summary>
    static class CommandPipe
    {
        const string PipeName = "TritonLauncher";

        public static void StartServer(Action<string> onCommand)
        {
            var thread = new Thread(() =>
            {
                while (true)
                {
                    try
                    {
                        using (var server = new NamedPipeServerStream(PipeName, PipeDirection.In, 1))
                        {
                            server.WaitForConnection();
                            using (var reader = new StreamReader(server))
                            {
                                string line;
                                while ((line = reader.ReadLine()) != null)
                                    if (line.Trim().Length > 0) onCommand(line.Trim());
                            }
                        }
                    }
                    catch
                    {
                        Thread.Sleep(500);
                    }
                }
            })
            { IsBackground = true, Name = "CommandPipe" };
            thread.Start();
        }

        public static bool Send(string command)
        {
            try
            {
                using (var client = new NamedPipeClientStream(".", PipeName, PipeDirection.Out))
                {
                    client.Connect(1500);
                    using (var writer = new StreamWriter(client))
                    {
                        writer.WriteLine(command);
                        writer.Flush();
                    }
                }
                return true;
            }
            catch
            {
                return false;
            }
        }
    }
}
