-- DataPilot 启动器：没运行就启动并打开页面；已运行就选择打开页面或关闭服务。
-- 由 scripts/app/make_app.sh 编译成 ~/Applications/DataPilot.app（__ROOT__ 会替换成项目路径）
property root : "__ROOT__"

on run
	set sh to "bash " & quoted form of (root & "/scripts/app.sh")
	set isRunning to true
	try
		do shell script sh & " status"
	on error
		set isRunning to false
	end try

	if isRunning then
		set choice to button returned of (display dialog "DataPilot 正在运行。" buttons {"关闭服务", "打开页面"} default button "打开页面" with title "DataPilot")
		if choice is "关闭服务" then
			display notification "正在关闭服务…" with title "DataPilot"
			try
				do shell script sh & " stop 2>&1"
			on error errMsg
				display dialog "关闭失败：" & return & errMsg buttons {"好"} default button "好" with title "DataPilot" with icon stop
				return
			end try
			display notification "已关闭，内存已释放" with title "DataPilot"
		else
			open location "http://localhost:3000"
		end if
	else
		display notification "正在启动，约 30 秒…" with title "DataPilot"
		try
			do shell script sh & " start 2>&1"
		on error errMsg
			display dialog "启动失败：" & return & errMsg buttons {"好"} default button "好" with title "DataPilot" with icon stop
			return
		end try
		open location "http://localhost:3000"
	end if
end run
